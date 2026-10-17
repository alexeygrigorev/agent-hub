"""Trusted Platform Role dispatch adapter (M8b).

Coordinates execution of automation role jobs (writer-run, principal-wake, head-recovery)
through the maintained Agent Quota Launcher native backend.

Contract:
- Requires opt-in machine-local trusted role configuration (roles.json), disabled by default.
- Allows only explicitly supported profiles: writer-run, principal-wake, head-recovery.
- Reuses existing runtime qualification, launcher task units, and fencing.
- Never calls raw model API / CLI outside maintained launcher.
- Never treats job-role profile names as model provider profiles.
- Never treats caller PID / transport receipt / process exit 0 as success.
- Requires genuine first-action evidence matching job_id, role_name, generation, and epoch.
- Bounded outcome artifact verification required; bare exit 0 or first action is refused.
- Fences and reports honestly when role authority expires, revokes, or changes.
- Treats model artifacts as private untrusted output until fenced host result acceptance.
- Never conflates role state directory with canonical root state directory.
- Bounded containment cancellation verified through native aplexer/process stop.
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
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import private_fs
from . import native_sessions as ns
from .native_sessions import NativeTarget, SessionControlError, SessionUncertain, StopResult, canonicalize_workspace

logger = logging.getLogger(__name__)

SUPPORTED_ROLE_PROFILES = ("writer-run", "principal-wake", "head-recovery")
DEFAULT_ROLES_CONFIG_PATH = "~/.config/agent-hub/roles.json"
DEFAULT_ROLE_STATE_DIR = "~/.local/state/agent-hub-host/roles"
DEFAULT_CANONICAL_ROOT_STATE_DIR = "~/.local/state/headless-root"
DEFAULT_LAUNCHER_REPO = "~/git/agent-quota-launcher"
DEFAULT_LAUNCHER_PROVIDER = "antigravity"
DEFAULT_ADMISSION_TASK_PROFILE = "model-task"
SUPPORTED_LAUNCHER_PROVIDERS = ("antigravity", "grok", "zai", "opencode")

MIN_TIMEOUT_SEC: float = 30.0
MAX_TIMEOUT_SEC: float = 1800.0
DEFAULT_TIMEOUT_SEC: float = 600.0

MIN_MEMORY_MB: int = 128
MAX_MEMORY_MB: int = 1500
DEFAULT_MEMORY_MB: int = 768

BOOTSTRAP_TIMEOUT_SEC: float = 120.0
MAX_CONFIG_BYTES: int = 65536
MAX_EVIDENCE_BYTES: int = 65536
MAX_OUTCOME_BYTES: int = 65536

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
NIL_UUID = "00000000-0000-0000-0000-000000000000"
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
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.monotonic_ns()}")
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


def safe_read_bounded_json(
    path: pathlib.Path | str | os.PathLike,
    max_bytes: int = MAX_EVIDENCE_BYTES,
    min_mtime: Optional[float] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[bytes]]:
    """Securely read and parse JSON file boundedly with descriptor-relative ancestor and leaf protection."""
    target = pathlib.Path(path)
    try:
        raw_bytes = private_fs.read_bytes(str(target), max_bytes, private=False)
        if min_mtime is not None:
            st = target.stat()
            if st.st_mtime < min_mtime - 0.05:
                return None, None, None
        digest = hashlib.sha256(raw_bytes).hexdigest()
        data = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(data, dict):
            return None, None, None
        return data, digest, raw_bytes
    except Exception:
        return None, None, None


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
    workspace: Optional[pathlib.Path] = None
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    memory_mb: int = DEFAULT_MEMORY_MB
    launcher_provider: str = DEFAULT_LAUNCHER_PROVIDER
    launcher_model: Optional[str] = "gemini-3.1-pro-high"
    launcher_effort: str = "high"
    launcher_task_profile: str = DEFAULT_ADMISSION_TASK_PROFILE
    entrypoint: Optional[pathlib.Path] = None

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
    launcher_config_dir: pathlib.Path
    launcher_argv: Tuple[str, ...]
    allowed_workspaces: Tuple[pathlib.Path, ...]
    allowed_models: Tuple[str, ...]
    allowed_accounts: Tuple[str, ...]
    config_mtime: float = 0.0
    config_hash: str = ""

    @classmethod
    def disabled(cls) -> "TrustedRoleConfig":
        sd = _resolve(expand_trusted_path(DEFAULT_ROLE_STATE_DIR))
        lr = _resolve(expand_trusted_path(DEFAULT_LAUNCHER_REPO))
        return cls(
            enabled=False,
            profiles={},
            state_dir=sd,
            launcher_repo=lr,
            launcher_config_dir=sd / "launcher",
            launcher_argv=(sys.executable, "-m", "launcher"),
            allowed_workspaces=(),
            allowed_models=(),
            allowed_accounts=(),
            config_mtime=0.0,
            config_hash="",
        )

    @classmethod
    def build(
        cls,
        enabled: bool = False,
        profiles: Optional[Dict[str, RoleProfileConfig]] = None,
        state_dir: Optional[str | os.PathLike] = None,
        launcher_repo: Optional[str | os.PathLike] = None,
        launcher_config_dir: Optional[str | os.PathLike] = None,
        launcher_argv: Optional[Sequence[str]] = None,
        allowed_workspaces: Optional[Sequence[str | os.PathLike]] = None,
        allowed_models: Optional[Sequence[str]] = None,
        allowed_accounts: Optional[Sequence[str]] = None,
        canonical_root_state_dir: Optional[str | os.PathLike] = None,
        config_mtime: float = 0.0,
        config_hash: str = "",
    ) -> "TrustedRoleConfig":
        sd = _resolve(expand_trusted_path(state_dir or DEFAULT_ROLE_STATE_DIR))
        canon_root = _resolve(expand_trusted_path(canonical_root_state_dir or DEFAULT_CANONICAL_ROOT_STATE_DIR))
        if _is_within(sd, canon_root) or _is_within(canon_root, sd):
            raise DispatchError("role_state_dir_conflated_with_canonical_root_state")

        lr = _resolve(expand_trusted_path(launcher_repo or DEFAULT_LAUNCHER_REPO))
        lcd = _resolve(expand_trusted_path(launcher_config_dir or (sd / "launcher")))
        l_argv = tuple(launcher_argv) if launcher_argv else (sys.executable, "-m", "launcher")

        ws_list = []
        for ws in allowed_workspaces or ():
            ws_list.append(validate_workspace(ws))

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
            launcher_config_dir=lcd,
            launcher_argv=l_argv,
            allowed_workspaces=tuple(ws_list),
            allowed_models=tuple(str(m) for m in (allowed_models or ())),
            allowed_accounts=tuple(str(a) for a in allowed_accounts) if allowed_accounts is not None else (),
            config_mtime=float(config_mtime),
            config_hash=str(config_hash),
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
            content = raw.read_bytes()
            cfg_hash = hashlib.sha256(content).hexdigest()
            data = json.loads(content.decode("utf-8"))
        except Exception as e:
            raise DispatchError(f"unreadable_role_config: {e}")

        if not isinstance(data, dict):
            raise DispatchError("role_config_not_dict")
        if data.get("version") != 1:
            raise DispatchError("unsupported_role_config_version")

        enabled = bool(data.get("enabled", False))
        state_dir_val = data.get("state_dir")
        launcher_repo_val = data.get("launcher_repo")
        launcher_config_dir_val = data.get("launcher_config_dir")
        launcher_argv_val = data.get("launcher_argv")
        allowed_workspaces_val = data.get("allowed_workspaces")
        allowed_models_val = data.get("allowed_models")
        allowed_accounts_val = data.get("allowed_accounts")

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
            launcher_provider = str(p_data.get("launcher_provider", DEFAULT_LAUNCHER_PROVIDER))
            launcher_model = p_data.get("launcher_model")
            launcher_effort = str(p_data.get("launcher_effort", "high"))
            launcher_task_profile = str(p_data.get("launcher_task_profile", DEFAULT_ADMISSION_TASK_PROFILE))

            parsed_profiles[prof_name] = RoleProfileConfig(
                profile=prof_name,
                enabled=p_enabled,
                entrypoint=pathlib.Path(entrypoint_val) if entrypoint_val else None,
                workspace=pathlib.Path(workspace_val) if workspace_val else None,
                timeout_sec=timeout_sec,
                memory_mb=memory_mb,
                launcher_provider=launcher_provider,
                launcher_model=launcher_model,
                launcher_effort=launcher_effort,
                launcher_task_profile=launcher_task_profile,
            )

        return cls.build(
            enabled=enabled,
            profiles=parsed_profiles,
            state_dir=state_dir_val,
            launcher_repo=launcher_repo_val,
            launcher_config_dir=launcher_config_dir_val,
            launcher_argv=launcher_argv_val,
            allowed_workspaces=allowed_workspaces_val,
            allowed_models=allowed_models_val,
            allowed_accounts=allowed_accounts_val,
            canonical_root_state_dir=canonical_root_state_dir,
            config_mtime=st.st_mtime,
            config_hash=cfg_hash,
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
                    launcher_provider=p_data.get("launcher_provider", DEFAULT_LAUNCHER_PROVIDER),
                    launcher_model=p_data.get("launcher_model"),
                    launcher_effort=p_data.get("launcher_effort", "high"),
                    launcher_task_profile=p_data.get("launcher_task_profile", DEFAULT_ADMISSION_TASK_PROFILE),
                )
        return TrustedRoleConfig.build(
            enabled=config.get("enabled", False),
            profiles=parsed_profiles,
            state_dir=config.get("state_dir"),
            launcher_repo=config.get("launcher_repo"),
            launcher_config_dir=config.get("launcher_config_dir"),
            launcher_argv=config.get("launcher_argv"),
            allowed_workspaces=config.get("allowed_workspaces"),
            allowed_models=config.get("allowed_models"),
            allowed_accounts=config.get("allowed_accounts"),
            canonical_root_state_dir=canonical_root_state_dir,
        )
    if isinstance(config, (str, os.PathLike)) or config is None:
        return TrustedRoleConfig.load(config, canonical_root_state_dir=canonical_root_state_dir)
    return None


class LauncherBackend:
    """Strict maintained AgentQuotaLauncher CLI invocation adapter."""

    def __init__(
        self,
        launcher_repo: pathlib.Path,
        config_dir: pathlib.Path,
        argv: Tuple[str, ...] = (sys.executable, "-m", "launcher"),
        aplexer_bin: str = "aplexer",
    ):
        self.launcher_repo = pathlib.Path(launcher_repo).resolve()
        self.config_dir = pathlib.Path(config_dir).resolve()
        self.argv = tuple(argv)
        self.aplexer_bin = aplexer_bin

    def plan(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute dry-run plan admission through maintained Launcher CLI."""
        cmd = list(self.argv) + [
            "--config-dir",
            str(self.config_dir),
            "plan",
            "--payload",
            json.dumps(payload),
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{self.launcher_repo}:{env.get('PYTHONPATH', '')}".rstrip(":")
        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20.0,
                env=env,
            )
            data = json.loads(res.stdout) if res.stdout else {}
            return data
        except Exception as e:
            return {"mode": "dry-run", "error": str(e), "chosen": None}

    def submit(
        self,
        task_id: str,
        idempotency_key: str,
        payload: Dict[str, Any],
        paths: Sequence[str] = (),
    ) -> Dict[str, Any]:
        """Idempotently submit task to maintained Launcher CLI."""
        cmd = list(self.argv) + [
            "--config-dir",
            str(self.config_dir),
            "submit",
            "--id",
            task_id,
            "--key",
            idempotency_key,
            "--payload",
            json.dumps(payload),
            "--json",
        ]
        if paths:
            cmd.extend(["--paths", ",".join(str(p) for p in paths)])
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{self.launcher_repo}:{env.get('PYTHONPATH', '')}".rstrip(":")
        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20.0,
                env=env,
            )
            return json.loads(res.stdout) if res.stdout else {"error": res.stderr.strip()}
        except Exception as e:
            return {"task_id": task_id, "error": str(e)}

    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Query maintained Launcher store for task status."""
        state_db = self.config_dir / "state.db"
        if state_db.is_file():
            try:
                import sqlite3
                with sqlite3.connect(str(state_db)) as conn:
                    cursor = conn.execute(
                        "SELECT id, idempotency_key, state, payload FROM tasks WHERE id = ?",
                        (task_id,),
                    )
                    row = cursor.fetchone()
                    if row:
                        payload = {}
                        if row[3]:
                            try:
                                payload = json.loads(row[3])
                            except Exception:
                                pass
                        return {
                            "id": row[0],
                            "task_id": row[0],
                            "idempotency_key": row[1],
                            "state": row[2],
                            "payload": payload,
                        }
            except Exception:
                pass

        cmd = list(self.argv) + [
            "--config-dir",
            str(self.config_dir),
            "status",
            "--id",
            task_id,
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{self.launcher_repo}:{env.get('PYTHONPATH', '')}".rstrip(":")
        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                timeout=5.0,
            )
            if res.returncode == 0 and res.stdout.strip():
                return json.loads(res.stdout)
        except Exception:
            pass
        return None

    def get_task_binding(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Query maintained Launcher store for exact native binding."""
        state_db = self.config_dir / "state.db"
        if state_db.is_file():
            try:
                import sqlite3
                with sqlite3.connect(str(state_db)) as conn:
                    cursor = conn.execute(
                        "SELECT native_id, tag, workspace, engine, incarnation, details "
                        "FROM task_native_bindings WHERE task_id = ?",
                        (task_id,),
                    )
                    row = cursor.fetchone()
                    if row and row[0]:
                        det = {}
                        if row[5]:
                            try:
                                det = json.loads(row[5])
                            except Exception:
                                pass
                        return {
                            "task_id": task_id,
                            "native_id": row[0],
                            "session_id": row[0],
                            "tag": row[1],
                            "workspace": row[2],
                            "engine": row[3],
                            "created_at_ms": row[4],
                            "incarnation": row[4],
                            "details": det,
                        }
            except Exception:
                pass

        cmd = list(self.argv) + [
            "--config-dir",
            str(self.config_dir),
            "status",
            "--id",
            task_id,
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{self.launcher_repo}:{env.get('PYTHONPATH', '')}".rstrip(":")
        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                timeout=5.0,
            )
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout)
                binding = data.get("native_binding")
                if binding and isinstance(binding, dict) and binding.get("native_id"):
                    return {
                        "task_id": task_id,
                        "native_id": binding["native_id"],
                        "session_id": binding["native_id"],
                        "tag": binding.get("tag"),
                        "workspace": binding.get("workspace"),
                        "engine": binding.get("engine"),
                        "created_at_ms": binding.get("incarnation"),
                        "incarnation": binding.get("incarnation"),
                        "details": binding.get("details", {}),
                    }
        except Exception:
            pass
        return None

    def submit(
        self,
        task_id: str,
        idempotency_key: str,
        payload: Dict[str, Any],
        paths: Optional[Sequence[os.PathLike | str]] = None,
    ) -> Dict[str, Any]:
        """Submit role task to maintained Launcher store with lost-startreply reconciliation."""
        existing = self.get_task_status(task_id)
        if existing and existing.get("id") == task_id:
            logger.info(f"Task {task_id} already exists in launcher store; reconciling without duplicate submit")
            return existing

        cmd = list(self.argv) + [
            "--config-dir",
            str(self.config_dir),
            "submit",
            "--id",
            task_id,
            "--key",
            idempotency_key,
            "--payload",
            json.dumps(payload),
            "--json",
        ]
        if paths:
            cmd.extend(["--paths", ",".join(str(p) for p in paths)])
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{self.launcher_repo}:{env.get('PYTHONPATH', '')}".rstrip(":")
        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20.0,
                env=env,
            )
            return json.loads(res.stdout) if res.stdout else {"error": res.stderr.strip()}
        except Exception as e:
            existing = self.get_task_status(task_id)
            if existing and existing.get("id") == task_id:
                return existing
            return {"task_id": task_id, "error": str(e)}

    def run(
        self,
        task_id: str,
        cwd: str,
        tmpdir: Optional[str] = None,
        timeout_sec: float = 1800.0,
    ) -> Tuple[int, Dict[str, Any], str]:
        """Execute task via maintained Launcher CLI run with explicit aplexer backend."""
        cmd = list(self.argv) + [
            "--config-dir",
            str(self.config_dir),
            "run",
            "--id",
            task_id,
            "--cwd",
            str(cwd),
            "--backend",
            "aplexer",
        ]
        if tmpdir:
            cmd.extend(["--tmpdir", str(tmpdir)])
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{self.launcher_repo}:{env.get('PYTHONPATH', '')}".rstrip(":")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            out, err = proc.communicate(timeout=timeout_sec)
            receipt = {}
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        doc = json.loads(line)
                        if doc.get("receipt"):
                            receipt = doc["receipt"]
                            break
                    except Exception:
                        pass
            return proc.returncode, receipt, out
        except Exception as e:
            return 1, {}, str(e)

    def stop_session(
        self,
        target: Union[NativeTarget, str],
        *,
        tag: Optional[str] = None,
        workspace: Optional[str] = None,
        created_at_ms: Optional[int] = None,
        engine: Optional[str] = None,
        signal: str = "HUP",
        grace_ms: int = 2000,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Verified containment stop via native_sessions.stop_session.

        Binds exact UUID + workspace + tag before effect. After kill, confirms native exit.
        An unknown status, live witness, or unproven exit returns False (uncertain hold).
        """
        if isinstance(target, str):
            if not target or not UUID_RE.match(target) or target == NIL_UUID:
                return False, "invalid_native_session_uuid", {}
            if not tag or not workspace:
                try:
                    candidates = [s for s in ns.list_sessions(self.aplexer_bin, include_exited=True)
                                  if s.session_id.lower() == target.lower()]
                    if candidates:
                        tag = tag or candidates[0].tag
                        workspace = workspace or candidates[0].workspace
                        created_at_ms = created_at_ms or candidates[0].created_at_ms
                        engine = engine or candidates[0].engine
                except Exception:
                    pass
            if not tag or not workspace:
                return False, "missing_tag_or_workspace_for_target", {}
            try:
                native_target = NativeTarget(
                    session_id=target,
                    tag=tag,
                    workspace=canonicalize_workspace(workspace),
                    created_at_ms=created_at_ms,
                    engine=engine,
                )
            except Exception as e:
                return False, f"invalid_target: {e}", {}
        elif isinstance(target, NativeTarget):
            native_target = target
        else:
            return False, "invalid_target_type", {}

        try:
            res = ns.stop_session(
                executable=self.aplexer_bin,
                target=native_target,
                signal=signal,
                grace_ms=grace_ms,
            )
            return True, res.reason, {"state": res.state, "session_id": res.session_id}
        except ns.SessionUncertain as e:
            # Uncertain is NEVER exit proof: preserve uncertain hold!
            return False, f"uncertain_containment: {e}", {"uncertain": True, "error": str(e)}
        except ns.SessionControlError as e:
            return False, f"control_error: {e}", {"error": str(e)}
        except Exception as e:
            return False, f"stop_failed: {e}", {"error": str(e)}


class RoleQualification:
    """Evaluates local role configuration, launcher admission plan, and provider quota."""

    def __init__(
        self,
        config: Optional[TrustedRoleConfig] = None,
        quota_checker: Optional[Callable[[], Tuple[bool, str]]] = None,
        backend: Optional[LauncherBackend] = None,
    ):
        self.config = config or TrustedRoleConfig.disabled()
        self.quota_checker = quota_checker
        self.backend = backend or LauncherBackend(
            launcher_repo=self.config.launcher_repo,
            config_dir=self.config.launcher_config_dir,
            argv=self.config.launcher_argv,
        )

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

        # Provider and model allowlists
        provider = p_cfg.launcher_provider
        if self.config.allowed_accounts and provider not in self.config.allowed_accounts:
            report["reasons"].append(f"provider_not_in_allowed_accounts: {provider}")
            return report

        if p_cfg.launcher_model and self.config.allowed_models:
            if p_cfg.launcher_model not in self.config.allowed_models:
                report["reasons"].append(f"model_not_in_allowed_models: {p_cfg.launcher_model}")
                return report

        # Quota and admission gate evaluation
        has_quota_evidence = False
        if self.quota_checker:
            try:
                ok, why = self.quota_checker()
                if not ok:
                    report["reasons"].append(f"quota_refused: {why}")
                    return report
                has_quota_evidence = True
            except Exception as e:
                report["reasons"].append(f"quota_check_failed: {e}")
                return report

        # Maintained plan admission check
        plan_ran = False
        if self.backend and (self.config.launcher_repo / "launcher").is_dir():
            plan_payload = {
                "goal": f"qualification probe for {kind}",
                "owner": f"hub-role-{kind}",
                "cwd": str(p_cfg.workspace or self.config.state_dir),
                "timeout": p_cfg.timeout_sec,
                "task_profile": p_cfg.launcher_task_profile,
                "model_requirements": {
                    "providers": [provider],
                    "models": [p_cfg.launcher_model] if p_cfg.launcher_model else [],
                },
            }
            plan_res = self.backend.plan(plan_payload)
            plan_ran = True
            if plan_res.get("chosen") is None or plan_res.get("error"):
                why = plan_res.get("error") or str(plan_res.get("rejections", {}))
                report["reasons"].append(f"launcher_admission_refused: {why}")
                return report

        if not has_quota_evidence and not plan_ran:
            report["reasons"].append("no_provider_qualification_available: quota_checker or launcher plan required")
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
    expected_native_session: Optional[str] = None,
) -> Optional[str]:
    """Validate custody and compute digest for role first-action evidence boundedly."""
    data: Optional[Dict[str, Any]] = None
    raw_bytes: Optional[bytes] = None

    if isinstance(evidence_path_or_content, (os.PathLike, str)) and pathlib.Path(evidence_path_or_content).exists():
        data, digest, raw_bytes = safe_read_bounded_json(
            evidence_path_or_content,
            max_bytes=MAX_EVIDENCE_BYTES,
            min_mtime=started_wall,
        )
        if not data or not digest or not raw_bytes:
            return None
    elif isinstance(evidence_path_or_content, (str, bytes)):
        raw_bytes = evidence_path_or_content.encode("utf-8") if isinstance(evidence_path_or_content, str) else evidence_path_or_content
        if len(raw_bytes) > MAX_EVIDENCE_BYTES:
            return None
        try:
            data = json.loads(raw_bytes.decode("utf-8"))
            if not isinstance(data, dict):
                return None
        except Exception:
            return None
    else:
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
    if not isinstance(native_session, str) or not UUID_RE.fullmatch(native_session) or native_session == NIL_UUID:
        return None
    if expected_native_session and native_session != expected_native_session:
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

    return hashlib.sha256(raw_bytes).hexdigest()


class RoleRun:
    """Manages one execution cycle of an approved role job through maintained Launcher."""

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
        launcher_backend: Optional[LauncherBackend] = None,
        launcher_executor: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ):
        self.job_id = job_id
        self.job_token = job_token
        self.lease_epoch = int(lease_epoch)
        self.role_binding = dict(role_binding)
        self.role_name = str(role_binding.get("name", ""))
        self.role_generation = int(role_binding.get("generation", 0))
        self.kind = kind
        self.payload = dict(payload)
        self.hub_url = hub_url
        self.host_id = host_id
        self.config = config
        self.launcher_backend = launcher_backend or LauncherBackend(
            launcher_repo=config.launcher_repo,
            config_dir=config.launcher_config_dir,
            argv=config.launcher_argv,
        )
        self.launcher_executor = launcher_executor

        # Derived idempotent task and idempotency key (uses full sanitized job_id to prevent collision on distinct jobs sharing first 16 chars)
        safe_job = re.sub(r"[^a-zA-Z0-9_-]", "-", job_id)
        self.task_id = f"hub-role-{kind}-{safe_job}-g{self.role_generation}-e{self.lease_epoch}"
        self.idempotency_key = f"hub-role-{job_id}-g{self.role_generation}-e{self.lease_epoch}"
        self.run_id = f"{job_id}-g{self.role_generation}-e{self.lease_epoch}"

        # Native session is strictly unpopulated until corroborated from Launcher start record
        self.native_session: Optional[str] = None
        self.native_tag: Optional[str] = None
        self.native_engine: Optional[str] = None
        self.native_workspace: Optional[str] = None
        self.native_incarnation: Optional[int] = None
        self.created_at_ms: Optional[int] = None

        self.profile_cfg = config.profiles.get(kind) or RoleProfileConfig(profile=kind)
        self.engine = self.profile_cfg.launcher_provider or DEFAULT_LAUNCHER_PROVIDER
        self.run_dir = ensure_private_dir(config.runs_dir / f"{kind}-{job_id}")
        self.first_action_file = self.run_dir / f"first-action-{job_id}.json"
        self.dispatcher_first_action_file = config.runs_dir / f"first-action-{job_id}.json"
        self.start_ack_file = self.run_dir / f"start-ack-{job_id}.json"
        self.outcome_file = self.run_dir / f"outcome-{job_id}.json"

        self.task_workspace = (
            validate_workspace(self.profile_cfg.workspace)
            if self.profile_cfg.workspace
            else self.run_dir
        )

        self._thread: Optional[threading.Thread] = None
        self._cancelled = False
        self._cancel_reason: Optional[str] = None
        self._start_time = time.time()
        self._exec_result: Dict[str, Any] = {}
        self._exec_exception: Optional[Exception] = None

    def start(self) -> None:
        """Launch role execution through maintained Launcher CLI."""
        # Durable conflict detection: pre-existing evidence is NEVER unlinked
        if self.first_action_file.exists() or self.dispatcher_first_action_file.exists():
            raise DispatchError(f"pre_existing_evidence_conflict: first-action evidence already exists for {self.job_id}")
        if self.outcome_file.exists():
            raise DispatchError(f"pre_existing_evidence_conflict: outcome file already exists for {self.job_id}")

        self._start_time = time.time()

        if self.launcher_executor is not None:
            def _run_injected():
                try:
                    res = self.launcher_executor({
                        "task_id": self.task_id,
                        "job_id": self.job_id,
                        "run_dir": str(self.run_dir),
                        "first_action_file": str(self.first_action_file),
                        "start_ack_file": str(self.start_ack_file),
                        "outcome_file": str(self.outcome_file),
                        "profile_cfg": self.profile_cfg,
                    })
                    self._exec_result = res or {}
                except Exception as e:
                    self._exec_exception = e

            self._thread = threading.Thread(target=_run_injected, name=f"role-exec-{self.job_id}", daemon=True)
            self._thread.start()
            return

        # Maintained Launcher CLI pipeline
        launcher_payload = {
            "goal": (
                f"Role execution for {self.kind} ({self.role_name}, generation {self.role_generation}, epoch {self.lease_epoch}).\n"
                f"Job parameters: {json.dumps(self.payload)}\n"
                f"Complete the task and write execution outcome JSON to {self.outcome_file}:\n"
                '{"status": "succeeded", "summary": "...", "evidence_refs": [...]}\n'
            ),
            "owner": f"hub-role-{self.kind}",
            "cwd": str(self.task_workspace),
            "timeout": self.profile_cfg.timeout_sec,
            "task_profile": self.profile_cfg.launcher_task_profile,
            "model_requirements": {
                "providers": [self.profile_cfg.launcher_provider],
                "models": [self.profile_cfg.launcher_model] if self.profile_cfg.launcher_model else [],
            },
        }

        # Idempotent task submit
        sub_res = self.launcher_backend.submit(
            self.task_id,
            self.idempotency_key,
            launcher_payload,
            paths=[str(self.outcome_file)],
        )
        if sub_res.get("error"):
            logger.warning(f"Launcher submit reported error: {sub_res.get('error')}")

        def _run_launcher():
            try:
                rc, receipt, stdout = self.launcher_backend.run(
                    self.task_id,
                    cwd=str(self.task_workspace),
                    tmpdir=str(self.run_dir / "tmp"),
                    timeout_sec=self.profile_cfg.timeout_sec,
                )
                self._exec_result = {"rc": rc, "receipt": receipt, "stdout": stdout}
            except Exception as e:
                self._exec_exception = e

        self._thread = threading.Thread(target=_run_launcher, name=f"launcher-run-{self.job_id}", daemon=True)
        self._thread.start()

    def corroborate_maintained_binding(self) -> Optional[Dict[str, Any]]:
        """Corroborate authoritative maintained binding from launcher store or qualified native catalog.

        SECURITY INVARIANTS:
        1. All 5 fields must be complete and valid: non-nil UUID, tag (task-<task_id>),
           canonical workspace, engine, incarnation > 0.
        2. Incomplete or mismatched candidate files/receipts are NEVER filled with guessed defaults.
        3. Authoritative maintained store binding and native catalog/retirement qualification required.
        4. Stays None (unknown) if binding is incomplete or mismatched.
        """
        expected_tag = f"task-{self.task_id}"
        canonical_ws = canonicalize_workspace(str(self.task_workspace))
        expected_engine = self.engine

        # Query authoritative maintained store
        store_binding = self.launcher_backend.get_task_binding(self.task_id)

        # Inspect candidate start record if present
        start_record_path = self.task_workspace / ".local" / f"launch-{self.task_id}-start.json"
        if not start_record_path.exists():
            start_record_path = self.run_dir / ".local" / f"launch-{self.task_id}-start.json"

        cand_data: Optional[Dict[str, Any]] = None
        if start_record_path.exists():
            cand_data, _, _ = safe_read_bounded_json(start_record_path, min_mtime=self._start_time)

        # If execution result has receipt with native binding
        if cand_data is None and self._exec_result:
            rec = self._exec_result.get("receipt", {})
            cand_data = rec.get("native_binding") or rec

        # Check candidate data completeness: missing fields are NEVER guessed!
        if cand_data and isinstance(cand_data, dict):
            cand_id = cand_data.get("native_id") or cand_data.get("session_id") or cand_data.get("id")
            cand_tag = cand_data.get("tag")
            cand_ws = cand_data.get("workspace")
            cand_engine = cand_data.get("engine")
            cand_inc = cand_data.get("created_at_ms") or cand_data.get("incarnation")

            if (
                not cand_id or not isinstance(cand_id, str) or not UUID_RE.match(cand_id) or cand_id == NIL_UUID
                or not cand_tag or cand_tag != expected_tag
                or not cand_ws or canonicalize_workspace(str(cand_ws)) != canonical_ws
                or not cand_engine or cand_engine != expected_engine
                or not isinstance(cand_inc, int) or cand_inc <= 0
            ):
                if not store_binding:
                    return None

        # Check store binding
        if store_binding and isinstance(store_binding, dict):
            s_id = store_binding.get("native_id") or store_binding.get("session_id")
            s_tag = store_binding.get("tag")
            s_ws = store_binding.get("workspace")
            s_engine = store_binding.get("engine")
            s_inc = store_binding.get("incarnation") or store_binding.get("created_at_ms")

            if (
                not s_id or not isinstance(s_id, str) or not UUID_RE.match(s_id) or s_id == NIL_UUID
                or not s_tag or s_tag != expected_tag
                or not s_ws or canonicalize_workspace(str(s_ws)) != canonical_ws
                or not s_engine or s_engine != expected_engine
                or not isinstance(s_inc, int) or s_inc <= 0
            ):
                return None

            if cand_data and isinstance(cand_data, dict):
                cand_id = cand_data.get("native_id") or cand_data.get("session_id") or cand_data.get("id")
                if cand_id and cand_id.lower() != s_id.lower():
                    return None

            return {
                "session_id": s_id,
                "tag": s_tag,
                "workspace": canonical_ws,
                "engine": s_engine,
                "created_at_ms": s_inc,
            }

        # Candidate complete check with native catalog qualification
        if cand_data and isinstance(cand_data, dict):
            cand_id = cand_data.get("native_id") or cand_data.get("session_id") or cand_data.get("id")
            cand_tag = cand_data.get("tag")
            cand_ws = cand_data.get("workspace")
            cand_engine = cand_data.get("engine")
            cand_inc = cand_data.get("created_at_ms") or cand_data.get("incarnation")
            if (
                isinstance(cand_id, str) and UUID_RE.match(cand_id) and cand_id != NIL_UUID
                and cand_tag == expected_tag
                and cand_ws and canonicalize_workspace(str(cand_ws)) == canonical_ws
                and cand_engine == expected_engine
                and isinstance(cand_inc, int) and cand_inc > 0
            ):
                try:
                    candidates = [
                        s for s in ns.list_sessions(self.launcher_backend.aplexer_bin, include_exited=True)
                        if s.session_id.lower() == cand_id.lower()
                        and s.tag == expected_tag
                        and canonicalize_workspace(s.workspace) == canonical_ws
                    ]
                    if candidates:
                        if candidates[0].created_at_ms != cand_inc or candidates[0].engine != expected_engine:
                            return None
                        return {
                            "session_id": cand_id,
                            "tag": expected_tag,
                            "workspace": canonical_ws,
                            "engine": expected_engine,
                            "created_at_ms": cand_inc,
                        }
                except Exception:
                    pass
                try:
                    from .retired_native import verify_native_retirement
                    target = NativeTarget(
                        session_id=cand_id,
                        tag=expected_tag,
                        workspace=canonical_ws,
                        created_at_ms=cand_inc,
                        engine=expected_engine,
                    )
                    retired, tomb, _ = verify_native_retirement(self.launcher_backend.aplexer_bin, target)
                    if retired and isinstance(tomb, dict):
                        return {
                            "session_id": cand_id,
                            "tag": expected_tag,
                            "workspace": canonical_ws,
                            "engine": expected_engine,
                            "created_at_ms": cand_inc,
                        }
                except Exception:
                    pass

        return None

    def wait_bootstrap(self, timeout: float = BOOTSTRAP_TIMEOUT_SEC) -> Optional[Dict[str, Any]]:
        """Wait for worker and Launcher to produce corroborated native binding and first-action evidence.

        SECURITY INVARIANTS:
        1. Native session identity is NEVER bootstrapped from the untrusted first-action file.
        2. Must corroborate authoritative maintained binding (complete UUID/tag/workspace/engine/incarnation) FIRST.
        3. If no authoritative maintained binding exists, first-action evidence is REFUSED.
        4. When binding is known, first-action native_session MUST match the corroborated binding.
        """
        deadline = time.time() + timeout

        while time.time() < deadline:
            if self._cancelled:
                return None

            # 1. Attempt to corroborate authoritative native session binding
            if self.native_session is None:
                binding = self.corroborate_maintained_binding()
                if binding:
                    self.native_session = binding["session_id"]
                    self.native_tag = binding["tag"]
                    self.native_workspace = binding["workspace"]
                    self.native_engine = binding["engine"]
                    self.created_at_ms = binding["created_at_ms"]
                    self.native_incarnation = binding["created_at_ms"]

            # 2. Check for verified first-action file ONLY IF native_session is corroborated!
            # If native_session is still None, DO NOT accept first action file!
            if self.native_session is not None:
                for target_fa in (self.first_action_file, self.dispatcher_first_action_file):
                    if target_fa.exists():
                        digest = get_role_first_action_digest(
                            target_fa,
                            expected_job_id=self.job_id,
                            expected_epoch=self.lease_epoch,
                            expected_role_name=self.role_name,
                            expected_generation=self.role_generation,
                            started_wall=self._start_time,
                            expected_native_session=self.native_session,
                        )
                        if digest:
                            data, _, _ = safe_read_bounded_json(target_fa)
                            return {
                                "digest": digest,
                                "path": str(target_fa),
                                "native_session": self.native_session,
                                "data": data,
                            }

            if self._thread and not self._thread.is_alive():
                # If thread has exited and native_session was never bound, fail closed immediately
                if self.native_session is None:
                    break

            time.sleep(0.02)

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
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            return not self._thread.is_alive()
        return True

    def cancel(self, reason: str) -> Dict[str, Any]:
        """Cancel and dispose the running role unit cleanly with containment verification."""
        self._cancelled = True
        self._cancel_reason = reason
        verified = True
        stop_detail: Dict[str, Any] = {}

        # 1. Stop native containment via exact NativeTarget binding
        if self.native_session:
            target = NativeTarget(
                session_id=self.native_session,
                tag=self.native_tag or self.task_id,
                workspace=self.native_workspace or canonicalize_workspace(str(self.task_workspace)),
                created_at_ms=self.created_at_ms,
                engine=self.native_engine or self.engine,
            )
            stopped, stop_msg, stop_detail = self.launcher_backend.stop_session(target)
            if not stopped:
                verified = False
        elif self._thread is not None and self._thread.is_alive():
            # Process was launched but native session was never corroborated:
            # cannot prove containment stop without native binding!
            verified = False
            stop_detail = {"error": "unbound_native_session: cannot prove containment stop without native binding"}

        # 2. Stop launcher caller thread
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                verified = False

        return {"verified": verified, "reason": reason, "detail": stop_detail}

    def outcome(self) -> Dict[str, Any]:
        """Produce final execution outcome safely. Bounded outcome artifact strictly required."""
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

        # Bounded outcome artifact read
        if self.outcome_file.exists():
            data, digest, _ = safe_read_bounded_json(self.outcome_file, max_bytes=MAX_OUTCOME_BYTES)
            if data and isinstance(data, dict):
                status = data.get("status", "failed")
                hub_status = "succeeded" if status == "succeeded" else "failed"
                summary = scrub(str(data.get("summary", "role run complete")), self.job_token)
                refs = [str(r) for r in data.get("evidence_refs", []) if isinstance(r, (str, os.PathLike))]
                if status == "succeeded" and not refs:
                    refs = [str(self.outcome_file)]
                return {
                    "status": status,
                    "hub_status": hub_status,
                    "summary": summary,
                    "evidence_refs": refs,
                }

        # Check injected result
        if self._exec_result and "status" in self._exec_result:
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

        # Missing or invalid outcome artifact: bare exit 0 is NEVER acceptance!
        return {
            "status": "failed",
            "hub_status": "failed",
            "summary": "missing_or_invalid_outcome_artifact: verified bounded outcome artifact was not produced",
            "evidence_refs": [],
        }


class RoleDispatchAdapter:
    """Dispatches role jobs through verified local role configuration and maintained Launcher."""

    def __init__(
        self,
        config: Optional[TrustedRoleConfig] = None,
        quota_checker: Optional[Callable[[], Tuple[bool, str]]] = None,
        launcher_backend: Optional[LauncherBackend] = None,
        launcher_executor: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ):
        self.config = config or TrustedRoleConfig.disabled()
        self.launcher_backend = launcher_backend or LauncherBackend(
            launcher_repo=self.config.launcher_repo,
            config_dir=self.config.launcher_config_dir,
            argv=self.config.launcher_argv,
        )
        self.qualification = RoleQualification(
            self.config,
            quota_checker=quota_checker,
            backend=self.launcher_backend,
        )
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
            launcher_backend=self.launcher_backend,
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
