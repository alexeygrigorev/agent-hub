"""Trusted Platform Role dispatch adapter (M8b).

Coordinates execution of automation role jobs (writer-run, principal-wake, head-recovery)
through the maintained Agent Quota Launcher native backend.

Contract:
- Requires opt-in machine-local trusted role configuration (roles.json), disabled by default.
- Allows only explicitly supported profiles: writer-run, principal-wake, head-recovery.
- Reuses existing runtime qualification, launcher task units, and fencing.
- Never calls raw model API / CLI outside maintained launcher.
- Never treats job-role profile names as model provider profiles.
- Never treats caller PID / transport receipt as success.
- Requires genuine first-action evidence matching job_id, role_name, generation, and epoch.
- Fences and reports honestly when role authority expires, revokes, or changes.
- Treats model artifacts as private untrusted output until fenced host result acceptance.
- Never conflates role state directory with canonical root state directory.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import logging
import os
import pathlib
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

SUPPORTED_ROLE_PROFILES = ("writer-run", "principal-wake", "head-recovery")
DEFAULT_ROLES_CONFIG_PATH = "~/.config/agent-hub/roles.json"
DEFAULT_ROLE_STATE_DIR = "~/.local/state/agent-hub-host/roles"
DEFAULT_CANONICAL_ROOT_STATE_DIR = "~/.local/state/headless-root"

MIN_TIMEOUT_SEC: float = 30.0
MAX_TIMEOUT_SEC: float = 1800.0
DEFAULT_TIMEOUT_SEC: float = 600.0

MIN_MEMORY_MB: int = 128
MAX_MEMORY_MB: int = 1500
DEFAULT_MEMORY_MB: int = 768

BOOTSTRAP_TIMEOUT_SEC: float = 120.0
MAX_CONFIG_BYTES: int = 65536
MAX_EVIDENCE_BYTES: int = 65536

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
ROLE_NAME_RE = re.compile(r"^automation:[a-z][a-z0-9-]{0,47}:[a-z][a-z0-9-]{0,47}$")


class DispatchError(RuntimeError):
    """Raised when trusted Role dispatch is refused or fails. Never hidden."""


def scrub(text: Any, *secrets: Optional[str]) -> str:
    out = str(text)
    for s in secrets:
        if isinstance(s, str) and s:
            out = out.replace(s, "[redacted]")
    return out


def _resolve(path: str | os.PathLike) -> pathlib.Path:
    return pathlib.Path(os.path.expanduser(str(path))).resolve()


def expand_trusted_path(raw: str | os.PathLike) -> str:
    s = str(raw)
    if s == "~" or s.startswith("~/"):
        s = os.path.expanduser(s)
    return s


def _is_within(child: pathlib.Path, parent: pathlib.Path) -> bool:
    try:
        c = child.resolve()
        p = parent.resolve()
        return c == p or p in c.parents
    except Exception:
        return False


def ensure_private_dir(path: pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(path)
    if path.is_symlink():
        raise DispatchError(f"private_dir_is_symlink: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise DispatchError(f"private_dir_not_owned_directory: {path}")
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def write_private_json(path: pathlib.Path, data: Dict[str, Any]) -> None:
    path = pathlib.Path(path)
    if path.is_symlink():
        raise DispatchError(f"private_file_is_symlink: {path}")
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    encoded = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def validate_entrypoint(script_path: str | os.PathLike) -> pathlib.Path:
    raw = expand_trusted_path(script_path)
    if not raw.startswith("/"):
        raise DispatchError(f"entrypoint_not_absolute: {raw}")
    if ".." in pathlib.PurePosixPath(raw).parts:
        raise DispatchError(f"entrypoint_contains_dotdot: {raw}")
    p = _resolve(raw)
    if str(p) != os.path.normpath(raw):
        raise DispatchError(f"entrypoint_symlink_or_unresolved: {raw} -> {p}")
    try:
        st = os.lstat(p)
    except OSError as e:
        raise DispatchError(f"entrypoint_unreadable: {p}: {e}")
    if not stat.S_ISREG(st.st_mode):
        raise DispatchError(f"entrypoint_not_regular_file: {p}")
    if not os.access(p, os.X_OK):
        raise DispatchError(f"entrypoint_not_executable: {p}")
    if st.st_uid not in (os.getuid(), 0):
        raise DispatchError(f"entrypoint_wrong_owner: {p} owned by {st.st_uid}")
    if st.st_mode & 0o002:
        raise DispatchError(f"entrypoint_world_writable: {p}")
    return p


def validate_workspace(workspace_path: str | os.PathLike) -> pathlib.Path:
    raw = expand_trusted_path(workspace_path)
    if not raw.startswith("/"):
        raise DispatchError(f"workspace_not_absolute: {raw}")
    if ".." in pathlib.PurePosixPath(raw).parts:
        raise DispatchError(f"workspace_contains_dotdot: {raw}")
    p = _resolve(raw)
    if str(p) != os.path.normpath(raw):
        raise DispatchError(f"workspace_symlink_or_unresolved: {raw} -> {p}")
    try:
        st = os.lstat(p)
    except OSError as e:
        raise DispatchError(f"workspace_unreadable: {p}: {e}")
    if not stat.S_ISDIR(st.st_mode):
        raise DispatchError(f"workspace_not_directory: {p}")
    return p


@dataclasses.dataclass(frozen=True)
class RoleProfileConfig:
    profile: str = ""
    enabled: bool = False
    entrypoint: Optional[pathlib.Path] = None
    workspace: Optional[pathlib.Path] = None
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    memory_mb: int = DEFAULT_MEMORY_MB
    launcher_provider: str = "codex"
    launcher_model: Optional[str] = None
    launcher_task_profile: str = "model-task"

    def __post_init__(self):
        if self.profile and self.profile not in SUPPORTED_ROLE_PROFILES:
            raise DispatchError(f"unsupported_role_profile: {self.profile}")
        if not (MIN_TIMEOUT_SEC <= self.timeout_sec <= MAX_TIMEOUT_SEC):
            raise DispatchError(f"timeout_out_of_bounds: {self.timeout_sec}")
        if not (MIN_MEMORY_MB <= self.memory_mb <= MAX_MEMORY_MB):
            raise DispatchError(f"memory_out_of_bounds: {self.memory_mb}")


@dataclasses.dataclass(frozen=True)
class TrustedRoleConfig:
    """Absolute, resolved machine-local role configuration (opt-in)."""

    enabled: bool
    profiles: Dict[str, RoleProfileConfig]
    state_dir: pathlib.Path
    launcher_repo: pathlib.Path

    @classmethod
    def disabled(cls) -> "TrustedRoleConfig":
        return cls(
            enabled=False,
            profiles={},
            state_dir=_resolve(expand_trusted_path(DEFAULT_ROLE_STATE_DIR)),
            launcher_repo=_resolve(expand_trusted_path("~/git/agent-quota-launcher")),
        )

    @classmethod
    def build(
        cls,
        enabled: bool = False,
        profiles: Optional[Dict[str, RoleProfileConfig]] = None,
        state_dir: Optional[str | os.PathLike] = None,
        launcher_repo: Optional[str | os.PathLike] = None,
        canonical_root_state_dir: Optional[str | os.PathLike] = None,
    ) -> "TrustedRoleConfig":
        sd = _resolve(expand_trusted_path(state_dir or DEFAULT_ROLE_STATE_DIR))
        canon_root = _resolve(expand_trusted_path(canonical_root_state_dir or DEFAULT_CANONICAL_ROOT_STATE_DIR))
        if _is_within(sd, canon_root) or _is_within(canon_root, sd):
            raise DispatchError("role_state_dir_conflated_with_canonical_root_state")

        lr = _resolve(expand_trusted_path(launcher_repo or "~/git/agent-quota-launcher"))
        prof_dict = {}
        for p_name, p_cfg in (profiles or {}).items():
            if p_name not in SUPPORTED_ROLE_PROFILES:
                raise DispatchError(f"unsupported_role_profile_in_config: {p_name}")
            if not isinstance(p_cfg, RoleProfileConfig):
                raise DispatchError(f"invalid_profile_config: {p_name}")
            if p_cfg.profile and p_cfg.profile != p_name:
                raise DispatchError(f"profile_name_mismatch: {p_name} != {p_cfg.profile}")
            if not p_cfg.profile:
                p_cfg = dataclasses.replace(p_cfg, profile=p_name)
            if p_cfg.entrypoint is not None:
                validate_entrypoint(p_cfg.entrypoint)
            if p_cfg.workspace is not None:
                validate_workspace(p_cfg.workspace)
            prof_dict[p_name] = p_cfg

        return cls(
            enabled=bool(enabled),
            profiles=prof_dict,
            state_dir=sd,
            launcher_repo=lr,
        )

    @classmethod
    def load(
        cls,
        config_path: Optional[str | os.PathLike] = None,
        canonical_root_state_dir: Optional[str | os.PathLike] = None,
    ) -> "TrustedRoleConfig":
        raw_str = expand_trusted_path(config_path or DEFAULT_ROLES_CONFIG_PATH)
        raw = pathlib.Path(raw_str)
        if os.path.islink(raw) or (raw.exists() and stat.S_ISLNK(os.lstat(raw).st_mode)):
            raise DispatchError(f"invalid_role_config_file: symlink forbidden: {raw}")
        if not raw.is_file():
            return cls.disabled()

        st = os.lstat(raw)
        if stat.S_ISLNK(st.st_mode) or st.st_size > MAX_CONFIG_BYTES:
            raise DispatchError(f"invalid_role_config_file: {raw}")

        try:
            content = raw.read_text(encoding="utf-8")
            data = json.loads(content)
        except Exception as e:
            raise DispatchError(f"unreadable_role_config: {e}")

        if not isinstance(data, dict):
            raise DispatchError("role_config_not_dict")
        if data.get("version") != 1:
            raise DispatchError("unsupported_role_config_version")

        enabled = bool(data.get("enabled", False))
        state_dir_val = data.get("state_dir")
        launcher_repo_val = data.get("launcher_repo")

        profiles_raw = data.get("profiles", {})
        if not isinstance(profiles_raw, dict):
            raise DispatchError("profiles_must_be_dict")

        parsed_profiles: Dict[str, RoleProfileConfig] = {}
        for prof_name, p_data in profiles_raw.items():
            if not isinstance(p_data, dict):
                raise DispatchError(f"profile_data_not_dict: {prof_name}")
            p_enabled = bool(p_data.get("enabled", False))
            entrypoint_val = p_data.get("entrypoint")
            workspace_val = p_data.get("workspace")
            timeout_sec = float(p_data.get("timeout_sec", DEFAULT_TIMEOUT_SEC))
            memory_mb = int(p_data.get("memory_mb", DEFAULT_MEMORY_MB))
            launcher_provider = str(p_data.get("launcher_provider", "codex"))
            launcher_model = p_data.get("launcher_model")
            launcher_task_profile = str(p_data.get("launcher_task_profile", "model-task"))

            parsed_profiles[prof_name] = RoleProfileConfig(
                profile=prof_name,
                enabled=p_enabled,
                entrypoint=pathlib.Path(entrypoint_val) if entrypoint_val else None,
                workspace=pathlib.Path(workspace_val) if workspace_val else None,
                timeout_sec=timeout_sec,
                memory_mb=memory_mb,
                launcher_provider=launcher_provider,
                launcher_model=launcher_model,
                launcher_task_profile=launcher_task_profile,
            )

        return cls.build(
            enabled=enabled,
            profiles=parsed_profiles,
            state_dir=state_dir_val,
            launcher_repo=launcher_repo_val,
            canonical_root_state_dir=canonical_root_state_dir,
        )

    @property
    def runs_dir(self) -> pathlib.Path:
        return self.state_dir / "runs"


def load_role_config(
    config: Any = None,
    canonical_root_state_dir: Optional[str | os.PathLike] = None,
) -> Optional[TrustedRoleConfig]:
    """Helper to load TrustedRoleConfig from path, dict, or existing instance."""
    if isinstance(config, TrustedRoleConfig):
        return config
    if isinstance(config, dict):
        parsed_profiles = {}
        for prof_name, p_data in config.get("profiles", {}).items():
            if isinstance(p_data, RoleProfileConfig):
                parsed_profiles[prof_name] = p_data
            elif isinstance(p_data, dict):
                parsed_profiles[prof_name] = RoleProfileConfig(
                    enabled=bool(p_data.get("enabled", False)),
                    entrypoint=p_data.get("entrypoint"),
                    workspace=p_data.get("workspace"),
                    launcher_provider=p_data.get("launcher_provider"),
                    launcher_model=p_data.get("launcher_model"),
                    launcher_task_profile=p_data.get("launcher_task_profile"),
                )
        return TrustedRoleConfig.build(
            enabled=config.get("enabled", False),
            profiles=parsed_profiles,
            state_dir=config.get("state_dir"),
            launcher_repo=config.get("launcher_repo"),
            canonical_root_state_dir=canonical_root_state_dir,
        )
    if isinstance(config, (str, os.PathLike)) or config is None:
        return TrustedRoleConfig.load(config, canonical_root_state_dir=canonical_root_state_dir)
    return None


class RoleQualification:
    """Evaluates local role configuration, launcher interfaces, and provider quota."""

    def __init__(
        self,
        config: Optional[TrustedRoleConfig] = None,
        quota_checker: Optional[Callable[[], Tuple[bool, str]]] = None,
    ):
        self.config = config or TrustedRoleConfig.disabled()
        self.quota_checker = quota_checker

    def inspect_profile(self, kind: str) -> Dict[str, Any]:
        report: Dict[str, Any] = {
            "kind": kind,
            "supported": False,
            "reasons": [],
            "config_enabled": self.config.enabled,
        }

        if kind not in SUPPORTED_ROLE_PROFILES:
            report["reasons"].append(f"unsupported_profile: {kind}")
            return report

        if not self.config.enabled:
            report["reasons"].append("role_configuration_disabled")
            return report

        p_cfg = self.config.profiles.get(kind)
        if not p_cfg or not p_cfg.enabled:
            report["reasons"].append(f"role_profile_not_enabled_in_config: {kind}")
            return report

        if not self.config.launcher_repo.is_dir():
            report["reasons"].append(f"launcher_repo_missing: {self.config.launcher_repo}")
            return report

        if p_cfg.entrypoint is not None:
            try:
                validate_entrypoint(p_cfg.entrypoint)
            except DispatchError as e:
                report["reasons"].append(f"entrypoint_invalid: {e}")
                return report

        if p_cfg.workspace is not None:
            try:
                validate_workspace(p_cfg.workspace)
            except DispatchError as e:
                report["reasons"].append(f"workspace_invalid: {e}")
                return report

        if self.quota_checker:
            try:
                ok, why = self.quota_checker()
                if not ok:
                    report["reasons"].append(f"quota_refused: {why}")
                    return report
            except Exception as e:
                report["reasons"].append(f"quota_check_failed: {e}")
                return report

        report["supported"] = True
        return report


def get_role_first_action_digest(
    evidence_path_or_content: str | bytes | os.PathLike,
    expected_job_id: str,
    expected_epoch: int,
    expected_role_name: str,
    expected_generation: int,
    started_wall: Optional[float] = None,
) -> Optional[str]:
    """Validate custody and compute digest for role first-action evidence."""
    if isinstance(evidence_path_or_content, (os.PathLike, str)):
        p = pathlib.Path(evidence_path_or_content)
        if p.exists() and p.is_file():
            try:
                if started_wall is not None and p.stat().st_mtime < started_wall - 0.05:
                    return None
                st = os.lstat(p)
                if stat.S_ISLNK(st.st_mode) or st.st_size > MAX_EVIDENCE_BYTES:
                    return None
                content = p.read_bytes()
            except Exception:
                return None
        elif isinstance(evidence_path_or_content, str):
            content = evidence_path_or_content.encode("utf-8")
        else:
            return None
    elif isinstance(evidence_path_or_content, bytes):
        content = evidence_path_or_content
    else:
        return None

    try:
        data = json.loads(content.decode("utf-8"))
        if not isinstance(data, dict):
            return None
    except Exception:
        return None

    pid_val = data.get("pid")
    if (
        type(pid_val) is not int
        or pid_val <= 1
        or pid_val == os.getpid()
        or pid_val == os.getppid()
        or pid_val == 65534
    ):
        return None

    try:
        os.kill(pid_val, 0)
    except OSError:
        return None

    if data.get("job_id") != expected_job_id:
        return None
    if data.get("epoch") != expected_epoch:
        return None
    if data.get("role_name") != expected_role_name:
        return None
    if data.get("generation") != expected_generation:
        return None
    if data.get("stage") != "first_action":
        return None

    native_session = data.get("native_session")
    if not isinstance(native_session, str) or not UUID_RE.fullmatch(native_session) or native_session == "00000000-0000-0000-0000-000000000000":
        return None

    utc_val = data.get("utc")
    if not isinstance(utc_val, str) or not utc_val.strip():
        return None
    try:
        utc_dt = datetime.datetime.fromisoformat(utc_val.replace("Z", "+00:00"))
        if utc_dt.tzinfo is None:
            return None
    except Exception:
        return None

    ns_val = data.get("private_pid_namespace")
    if not isinstance(ns_val, str) or not re.match(r"^pid:\[[0-9]+\]$", ns_val) or ns_val == "pid:[unknown]":
        return None

    procs = data.get("numeric_proc_entries")
    if not isinstance(procs, list) or not all(type(x) is int and x > 0 for x in procs) or pid_val not in procs:
        return None

    return hashlib.sha256(content).hexdigest()


class RoleRun:
    """Manages one execution cycle of an approved role job."""

    def __init__(
        self,
        job_id: str,
        job_token: str,
        lease_epoch: int,
        role_binding: Dict[str, Any],
        kind: str,
        payload: Dict[str, Any],
        hub_url: str,
        host_id: str,
        config: TrustedRoleConfig,
        launcher_executor: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ):
        self.job_id = job_id
        self.job_token = job_token
        self.lease_epoch = lease_epoch
        self.role_binding = dict(role_binding)
        self.kind = kind
        self.payload = dict(payload)
        self.hub_url = hub_url
        self.host_id = host_id
        self.config = config
        self.launcher_executor = launcher_executor

        self.run_id = str(uuid.uuid4())
        self.task_id = f"hub-role-{kind}-{job_id[:16]}"
        self.native_session = str(uuid.uuid4())

        self.profile_cfg = config.profiles.get(kind) or RoleProfileConfig(profile=kind)
        self.run_dir = ensure_private_dir(config.runs_dir / f"{kind}-{job_id}")
        self.first_action_file = self.run_dir / f"first-action-{job_id}.json"
        self.start_ack_file = self.run_dir / f"start-ack-{job_id}.json"
        self.outcome_file = self.run_dir / f"outcome-{job_id}.json"

        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._cancelled = False
        self._cancel_reason: Optional[str] = None
        self._start_time = time.time()
        self._exec_result: Dict[str, Any] = {}
        self._exec_exception: Optional[Exception] = None

    def start(self) -> None:
        """Launch the role unit asynchronously."""
        if self.first_action_file.exists():
            try:
                self.first_action_file.unlink()
            except OSError:
                pass

        env = {
            "HUB_JOB_ID": self.job_id,
            "HUB_JOB_TOKEN": self.job_token,
            "HUB_LEASE_EPOCH": str(self.lease_epoch),
            "HUB_ROLE_NAME": str(self.role_binding.get("name", "")),
            "HUB_ROLE_GENERATION": str(self.role_binding.get("generation", 0)),
            "HUB_RUN_ID": self.run_id,
            "HUB_NATIVE_SESSION": self.native_session,
            "HUB_FIRST_ACTION_FILE": str(self.first_action_file),
            "HUB_START_ACK_FILE": str(self.start_ack_file),
            "HUB_OUTCOME_FILE": str(self.outcome_file),
            "HUB_URL": self.hub_url,
            "HUB_HOST_ID": self.host_id,
            "HUB_ROLE_KIND": self.kind,
        }

        if self.launcher_executor is not None:
            def _run():
                try:
                    res = self.launcher_executor({
                        "task_id": self.task_id,
                        "job_id": self.job_id,
                        "env": env,
                        "run_dir": str(self.run_dir),
                        "first_action_file": str(self.first_action_file),
                        "start_ack_file": str(self.start_ack_file),
                        "outcome_file": str(self.outcome_file),
                        "profile_cfg": self.profile_cfg,
                    })
                    self._exec_result = res or {}
                except Exception as e:
                    self._exec_exception = e

            self._thread = threading.Thread(target=_run, name=f"role-exec-{self.job_id}", daemon=True)
            self._thread.start()
            return

        cmd: List[str]
        if self.profile_cfg.entrypoint:
            cmd = [str(self.profile_cfg.entrypoint)]
        else:
            raise DispatchError(f"no_entrypoint_configured_for_profile: {self.kind}")

        run_env = os.environ.copy()
        run_env.update(env)
        cwd = str(self.profile_cfg.workspace) if self.profile_cfg.workspace else str(self.run_dir)

        self._process = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=run_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def wait_bootstrap(self, timeout: float = BOOTSTRAP_TIMEOUT_SEC) -> Optional[Dict[str, Any]]:
        """Wait for worker to emit verified first-action evidence."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._cancelled:
                return None
            if self.first_action_file.exists():
                digest = get_role_first_action_digest(
                    self.first_action_file,
                    expected_job_id=self.job_id,
                    expected_epoch=self.lease_epoch,
                    expected_role_name=self.role_binding.get("name", ""),
                    expected_generation=self.role_binding.get("generation", 0),
                    started_wall=self._start_time,
                )
                if digest:
                    try:
                        raw = json.loads(self.first_action_file.read_text(encoding="utf-8"))
                        return {
                            "digest": digest,
                            "path": str(self.first_action_file),
                            "native_session": raw.get("native_session", self.native_session),
                            "data": raw,
                        }
                    except Exception:
                        return None
            if self._process and self._process.poll() is not None:
                break
            if self._thread and not self._thread.is_alive():
                break
            time.sleep(0.01)
        return None

    def acknowledge_start(self, start_receipt: Dict[str, Any]) -> None:
        """Write start acknowledgement file to unblock business execution."""
        write_private_json(self.start_ack_file, {
            "status": "started",
            "job_id": self.job_id,
            "epoch": self.lease_epoch,
            "receipt": start_receipt,
            "ack_time": time.time(),
        })

    def wait_done(self, timeout: float = 1.0) -> bool:
        """Check if execution is completed."""
        if self._process is not None:
            return self._process.poll() is not None
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            return not self._thread.is_alive()
        return True

    def cancel(self, reason: str) -> Dict[str, Any]:
        """Cancel and dispose the running role unit cleanly."""
        self._cancelled = True
        self._cancel_reason = reason
        verified = True

        if self._process is not None and self._process.poll() is None:
            try:
                self._process.terminate()
                self._process.wait(timeout=2.0)
            except Exception:
                try:
                    self._process.kill()
                    self._process.wait(timeout=1.0)
                except Exception:
                    verified = False

        return {"verified": verified, "reason": reason}

    def outcome(self) -> Dict[str, Any]:
        """Produce final execution outcome safely."""
        if self._cancelled:
            return {
                "status": "failed",
                "hub_status": "failed",
                "summary": f"cancelled: {self._cancel_reason}",
                "evidence_refs": [],
            }

        if self._exec_exception:
            return {
                "status": "failed",
                "hub_status": "failed",
                "summary": f"execution_error: {self._exec_exception}",
                "evidence_refs": [],
            }

        if self.outcome_file.exists():
            try:
                data = json.loads(self.outcome_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    status = data.get("status", "failed")
                    hub_status = "succeeded" if status == "succeeded" else "failed"
                    summary = scrub(str(data.get("summary", "role run complete")), self.job_token)
                    refs = [str(r) for r in data.get("evidence_refs", []) if isinstance(r, (str, os.PathLike))]
                    return {
                        "status": status,
                        "hub_status": hub_status,
                        "summary": summary,
                        "evidence_refs": refs,
                    }
            except Exception:
                pass

        if self._exec_result:
            status = self._exec_result.get("status", "failed")
            hub_status = "succeeded" if status == "succeeded" else "failed"
            summary = scrub(str(self._exec_result.get("summary", "role run complete")), self.job_token)
            refs = [str(r) for r in self._exec_result.get("evidence_refs", [])]
            return {
                "status": status,
                "hub_status": hub_status,
                "summary": summary,
                "evidence_refs": refs,
            }

        if self._process is not None:
            rc = self._process.poll()
            if rc == 0:
                return {
                    "status": "succeeded",
                    "hub_status": "succeeded",
                    "summary": f"role {self.kind} completed successfully",
                    "evidence_refs": [str(self.first_action_file)],
                }
            return {
                "status": "failed",
                "hub_status": "failed",
                "summary": f"process_exited: rc={rc}",
                "evidence_refs": [],
            }

        return {
            "status": "failed",
            "hub_status": "failed",
            "summary": "unknown_outcome",
            "evidence_refs": [],
        }


class RoleDispatchAdapter:
    """Dispatches role jobs through verified local role configuration."""

    def __init__(
        self,
        config: Optional[TrustedRoleConfig] = None,
        quota_checker: Optional[Callable[[], Tuple[bool, str]]] = None,
        launcher_executor: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ):
        self.config = config or TrustedRoleConfig.disabled()
        self.qualification = RoleQualification(self.config, quota_checker=quota_checker)
        self.launcher_executor = launcher_executor

    def can_process(self, kind: str) -> Tuple[bool, str, Dict[str, Any]]:
        report = self.qualification.inspect_profile(kind)
        if report["supported"]:
            return True, "role_qualified", report
        reasons = "; ".join(report.get("reasons", [])) or "unsupported"
        return False, f"role_unqualified: {reasons}", report

    def begin(
        self,
        job_id: str,
        job_token: str,
        lease_epoch: int,
        role_binding: Dict[str, Any],
        kind: str,
        payload: Dict[str, Any],
        hub_url: str,
        host_id: str,
    ) -> RoleRun:
        ok, reason, _ = self.can_process(kind)
        if not ok:
            raise DispatchError(f"Refusing to execute {kind}: {reason}. Direct model/API bypass forbidden.")

        run = RoleRun(
            job_id=job_id,
            job_token=job_token,
            lease_epoch=lease_epoch,
            role_binding=role_binding,
            kind=kind,
            payload=payload,
            hub_url=hub_url,
            host_id=host_id,
            config=self.config,
            launcher_executor=self.launcher_executor,
        )
        run.start()
        return run

    def dispatch(
        self,
        job_id: str,
        kind: str,
        payload: Dict[str, Any],
        job_token: str,
        lease_epoch: int,
        role_binding: Dict[str, Any],
        hub_url: str,
        host_id: str,
        start_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
        stop_event: Optional[threading.Event] = None,
        deadline_sec: Optional[float] = None,
    ) -> Dict[str, Any]:
        """High-level dispatch of one role job."""
        run = self.begin(job_id, job_token, lease_epoch, role_binding, kind, payload, hub_url, host_id)

        ev = run.wait_bootstrap(BOOTSTRAP_TIMEOUT_SEC)
        if ev is None:
            run.cancel("bootstrap_failed_or_timeout")
            out = run.outcome()
            return {"status": "failed", "summary": out["summary"], "evidence_refs": []}

        if start_callback:
            start_reply = start_callback(ev)
            run.acknowledge_start(start_reply or {})

        event = stop_event or threading.Event()
        while not run.wait_done(0.5):
            now = time.time()
            if event.is_set():
                run.cancel("lease_lost_or_authority_changed")
                return {"status": "failed", "summary": "role authority lost or cancelled", "evidence_refs": [ev["path"]]}
            if deadline_sec is not None and now > deadline_sec:
                run.cancel("deadline_exceeded")
                return {"status": "failed", "summary": "deadline_exceeded: execution timed out", "evidence_refs": [ev["path"]]}

        out = run.outcome()
        return {
            "status": out["status"],
            "summary": out["summary"],
            "evidence_refs": out["evidence_refs"],
        }
