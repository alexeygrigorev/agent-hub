"""Trusted Root dispatch adapter: operational root-run execution through the
maintained Agent Quota Launcher task-unit API.

Contract (docs/ROOT-DISPATCH.md):
- The CAG script ``scripts/root/headless-root-run.sh`` owns the canonical
  ``root.lock`` / ``state.json`` / epoch contract. The host never takes that
  lock, not even as a non-blocking probe (a probe would make the timer's own
  ``flock -n`` see a false busy holder): lock state is read from
  ``/proc/locks``.
- Dispatch is one ``RootRun`` session: a fresh run id is pre-assigned by the
  host (``HUB_EXPECTED_RUN_ID``), the launcher unit is started with the private
  per-job environment, and only a receipt PRODUCED BY THAT RUN (job / epochs /
  task / run / unit / invocation / host / entrypoint hash / time, cross-checked
  against ``state.json`` and live ``/proc``) is accepted as first action. Foreign,
  stale, no-op or synthetic state yields zero effects.
- The script's bootstrap receipt (``observed_root_bootstrap``) is distinct from
  the model's first tool call (``observed_native_tool``); neither is derived
  from state or PID surrogates. Business execution waits for the host's start
  acknowledgement file, written only after the Hub accepted ``start``.
- The outcome is derived from the run's own ``runs/<run_id>.json``; a skip is
  ``skipped`` and never a successful cycle. Any host-side failure or timeout
  cancels and waits only the exact owned unit/invocation.
- Scheduling is a live authenticated Hub read per new run; a local file is never
  authority.
- Qualification needs pins (accepted CAG script hash and launcher commit) that
  are committed constants below. They are empty until the head accepts them, so
  the real adapter stays UNSUPPORTED/UNKNOWN today.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import inspect
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from .guarded_root import (
    HB_STALE_SEC,
    WINDOW_SEC,
    GRACE_SEC,
    get_current_pid_namespace,
    get_pid_starttime_ticks,
    is_pid_alive,
)

DEFAULT_SCRIPT_PATH = "~/git/cloudflare-agent-git/scripts/root/headless-root-run.sh"
DEFAULT_ALLOWED_REPO_ROOT = "~/git/cloudflare-agent-git"
DEFAULT_CANONICAL_STATE_DIR = "~/.local/state/headless-root"
DEFAULT_HOST_STATE_DIR = "~/.local/state/agent-hub-host"
SCHEDULING_FILENAME = "scheduling.json"
SCHEDULING_PATH = "/api/root/scheduling"
BOOTSTRAP_TIMEOUT_SEC = 240.0

ROOT_PROFILE = "root-run"
ROOT_PROVIDER = "codex"
ROOT_MEMORY_MB = 1500
RECEIPT_SCHEMA_VERSION = 1
RECEIPT_KINDS = ("observed_root_bootstrap", "observed_native_tool")
MAX_RECEIPT_BYTES = 64 * 1024

# Trust roots, committed in source and reviewed. Empty until the CAG script
# hooks and the Quota Launcher Root profile are accepted and pinned by the head.
ACCEPTED_CAG_SCRIPT_SHA256: frozenset = frozenset()
ACCEPTED_LAUNCHER_COMMITS: frozenset = frozenset()

# Keyword parameters the installed ``execute_transient_task_unit`` must name
# explicitly (a ``**kwargs`` catch-all does not count) before it can carry Root.
REQUIRED_EXEC_PARAMS = (
    "task_id", "command_argv", "memory_mb", "workspace", "tmpdir", "timeout_sec",
    "quse_json", "provider", "check_capacity", "profile",
)
ENV_PARAM_CHOICES = ("job_env", "job_env_file")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class DispatchError(RuntimeError):
    """Raised when trusted Root dispatch is refused or fails. Never hidden."""


def scrub(text: Any, *secrets: Optional[str]) -> str:
    out = str(text)
    for s in secrets:
        if isinstance(s, str) and s:
            out = out.replace(s, "[redacted]")
    return out


# ---------------------------------------------------------------------------
# Trusted configuration and entrypoint validation (B4)
# ---------------------------------------------------------------------------

def _resolve(path: str | os.PathLike) -> pathlib.Path:
    return pathlib.Path(os.path.expanduser(str(path))).resolve()


def expand_trusted_path(raw: str | os.PathLike) -> str:
    """Expand a configured path: absolute, or ``~`` / ``~/...`` (never ``~user``)."""
    s = str(raw)
    if s == "~" or s.startswith("~/"):
        s = os.path.expanduser(s)
    return s


def validate_entrypoint(
    script_path: str | os.PathLike,
    allowed_repo_root: str | os.PathLike = DEFAULT_ALLOWED_REPO_ROOT,
) -> pathlib.Path:
    """Validate a strict rooted absolute entrypoint for the root script.

    ``~/`` is expanded first (the configured default is home-relative). The
    expanded path must be absolute, free of ``..``, free of symlinks in every
    component, a regular executable file owned by this user (or root) that is
    not world-writable, under the maintained CAG repository root.
    """
    raw = expand_trusted_path(script_path)
    if not raw.startswith("/"):
        raise DispatchError(f"entrypoint_not_absolute: {raw}")
    if ".." in pathlib.PurePosixPath(raw).parts:
        raise DispatchError(f"entrypoint_contains_dotdot: {raw}")
    p = _resolve(raw)
    if str(p) != os.path.normpath(raw):
        raise DispatchError(f"entrypoint_symlink_or_unresolved: {raw} -> {p}")
    repo_root = _resolve(expand_trusted_path(allowed_repo_root))
    if repo_root not in p.parents:
        raise DispatchError(f"entrypoint_outside_repo_root: {p} not under {repo_root}")
    try:
        st = os.lstat(p)
    except OSError as e:
        raise DispatchError(f"entrypoint_unreadable: {p}: {e}")
    if not stat.S_ISREG(st.st_mode):
        raise DispatchError(f"entrypoint_not_regular_file: {p}")
    if not os.access(p, os.X_OK):
        raise DispatchError(f"entrypoint_not_executable: {p}")
    if st.st_uid not in (os.getuid(), 0):
        raise DispatchError(f"entrypoint_foreign_owner: {p}")
    if st.st_mode & 0o002:
        raise DispatchError(f"entrypoint_writable_by_others: {p}")
    return p


def file_sha256(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_within(child: pathlib.Path, parent: pathlib.Path) -> bool:
    return child == parent or parent in child.parents


@dataclasses.dataclass(frozen=True)
class TrustedRootConfig:
    """Absolute, resolved Root paths shared by qualification and the adapter."""

    script_path: pathlib.Path
    repo_root: pathlib.Path
    canonical_state_dir: pathlib.Path
    host_state_dir: pathlib.Path

    @classmethod
    def build(
        cls,
        script_path: str | os.PathLike | None = None,
        allowed_repo_root: str | os.PathLike | None = None,
        canonical_state_dir: str | os.PathLike | None = None,
        host_state_dir: str | os.PathLike | None = None,
    ) -> "TrustedRootConfig":
        canonical = canonical_state_dir or os.environ.get("HR_STATE") or DEFAULT_CANONICAL_STATE_DIR
        cfg = cls(
            script_path=pathlib.Path(expand_trusted_path(script_path or DEFAULT_SCRIPT_PATH)),
            repo_root=_resolve(expand_trusted_path(allowed_repo_root or DEFAULT_ALLOWED_REPO_ROOT)),
            canonical_state_dir=_resolve(expand_trusted_path(canonical)),
            host_state_dir=_resolve(expand_trusted_path(host_state_dir or DEFAULT_HOST_STATE_DIR)),
        )
        if _is_within(cfg.host_state_dir, cfg.canonical_state_dir) or _is_within(
            cfg.canonical_state_dir, cfg.host_state_dir
        ):
            raise DispatchError("host_state_dir_conflated_with_canonical_root_state")
        return cfg

    def validated_entrypoint(self) -> pathlib.Path:
        return validate_entrypoint(self.script_path, self.repo_root)

    @property
    def runs_dir(self) -> pathlib.Path:
        return self.canonical_state_dir / "runs"


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def parse_aware_iso(value: Any) -> float:
    """Epoch seconds of a timezone-aware ISO-8601 string; ValueError otherwise."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty string")
    s = value.strip()
    dt = datetime.datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
    if dt.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return dt.timestamp()


def iso_utc(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).isoformat()


def _clock_time(clock: Any) -> float:
    return clock.time() if hasattr(clock, "time") else time.time()


# ---------------------------------------------------------------------------
# Private, atomically written, non-symlink files (cache, ack)
# ---------------------------------------------------------------------------

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
    tmp = path.parent / f".{path.name}.tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_private_json(path: pathlib.Path, max_bytes: int = MAX_RECEIPT_BYTES) -> Dict[str, Any]:
    """Read a JSON object from a regular, same-owner, non-world-writable file (the dirs are private)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise
    except OSError as e:
        raise DispatchError(f"file_unreadable: {path}: {e}")
    if not stat.S_ISREG(st.st_mode):
        raise DispatchError(f"file_not_regular: {path}")
    if st.st_uid != os.getuid():
        raise DispatchError(f"file_foreign_owner: {path}")
    if st.st_mode & 0o002:
        raise DispatchError(f"file_writable_by_others: {path}")
    if st.st_size > max_bytes:
        raise DispatchError(f"file_too_large: {path}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        raw = f.read(max_bytes + 1)
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise DispatchError(f"file_corrupt_json: {path}")
    if not isinstance(data, dict):
        raise DispatchError(f"file_not_object: {path}")
    return data


# ---------------------------------------------------------------------------
# Scheduling: live authenticated Hub read (B6)
# ---------------------------------------------------------------------------

class SchedulingCache:
    """Non-authoritative record of the last VERIFIED Hub read.

    Lives in the host cache dir, never in the canonical Root state dir. A cache
    file can only add a denial; it can never grant dispatch, because any local
    process could write one.
    """

    def __init__(self, cache_dir: str | os.PathLike | None = None, ttl_sec: float = 120.0):
        # Deliberately NOT resolved: a symlinked cache dir must be seen and refused.
        self.dir = pathlib.Path(os.path.abspath(expand_trusted_path(cache_dir or DEFAULT_HOST_STATE_DIR)))
        self.path = self.dir / SCHEDULING_FILENAME
        self.ttl_sec = ttl_sec

    def store(self, record: Dict[str, Any]) -> None:
        ensure_private_dir(self.dir)
        write_private_json(self.path, record)

    def load_denial(self, now: float) -> Optional[str]:
        """Return a denial reason when a fresh, well-formed record says disabled."""
        try:
            data = read_private_json(self.path, 8192)
            captured = parse_aware_iso(data.get("captured_at"))
        except Exception:
            return None
        if type(data.get("enabled")) is not bool or data["enabled"]:
            return None
        if not (0.0 <= now - captured <= self.ttl_sec):
            return None
        return "scheduling_disabled: hub (cached)"


class HubSchedulingReader:
    """Verified, fresh read of ``GET /api/root/scheduling`` per new Root run.

    Credential: the paired host token, or a job token when the caller has one.
    Same origin as the paired hub only; redirects are refused. Every decision is
    a live read: the response must be a strict object with a boolean ``enabled``
    and a timezone-aware ``server_time`` close to local wall time and not behind
    a previously accepted ``server_time``. Missing, unreachable, unauthorized,
    stale, regressed or corrupt answers deny.
    """

    def __init__(
        self,
        base_url: str,
        token_provider: Callable[[], str],
        host_id: str = "",
        clock: Any = None,
        cache: Optional[SchedulingCache] = None,
        canonical_state_dir: str | os.PathLike | None = None,
        http_get: Optional[Callable[[str, str, float], Dict[str, Any]]] = None,
        max_skew_sec: float = 120.0,
        ttl_sec: float = 30.0,
        timeout_sec: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/") if isinstance(base_url, str) else base_url
        self.token_provider = token_provider
        self.host_id = host_id
        self.clock = clock or time
        self.cache = cache
        self.max_skew_sec = max_skew_sec
        self.ttl_sec = ttl_sec
        self.timeout_sec = timeout_sec
        self._http_get = http_get or self._default_http_get
        self._last_server_time: Optional[float] = None
        self._observed: Optional[Tuple[float, bool]] = None  # (monotonic, enabled)
        if cache is not None:
            canon = _resolve(expand_trusted_path(
                canonical_state_dir or os.environ.get("HR_STATE") or DEFAULT_CANONICAL_STATE_DIR))
            cache_real = cache.dir.resolve()
            if _is_within(cache_real, canon) or _is_within(canon, cache_real):
                raise DispatchError("host_state_dir_conflated_with_canonical_root_state")

    @staticmethod
    def _default_http_get(url: str, token: str, timeout: float) -> Dict[str, Any]:
        from . import api
        api.check_url(url)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="GET")
        return api.safe_http_json(req, timeout=timeout)

    @property
    def url(self) -> str:
        return f"{self.base_url}{SCHEDULING_PATH}"

    def last_observation(self) -> Optional[bool]:
        """Diagnostic only: the in-process verified value while within the monotonic TTL."""
        if self._observed is None:
            return None
        mono, enabled = self._observed
        return enabled if 0.0 <= time.monotonic() - mono <= self.ttl_sec else None

    def check(self, job_token: Optional[str] = None) -> Tuple[bool, str]:
        """Live verified read. A job credential is tried first when given; if the Hub refuses
        it (401/403) the paired host credential is tried. Either way the answer is verified."""
        tokens: List[str] = []
        for provider in (lambda: job_token, self.token_provider):
            try:
                t = provider()
            except Exception:
                t = None
            if isinstance(t, str) and t.strip() and t.strip() not in tokens:
                tokens.append(t.strip())
        if not tokens:
            return False, "scheduling_credential_missing"
        now = _clock_time(self.clock)
        data: Any = None
        for i, token in enumerate(tokens):
            try:
                data = self._http_get(self.url, token, self.timeout_sec)
                break
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    if i + 1 < len(tokens):
                        continue
                    return False, (f"scheduling_unauthorized: http {e.code}; "
                                   f"scoped host read of {SCHEDULING_PATH} required")
                if e.code == 404:
                    return False, "scheduling_endpoint_absent"
                return False, f"scheduling_http_error: {e.code}"
            except Exception as e:
                cached = self.cache.load_denial(now) if self.cache is not None else None
                return False, cached or f"scheduling_unreachable: {type(e).__name__}"
        return self._verify(data, now)

    def _verify(self, data: Any, now: float) -> Tuple[bool, str]:
        if not isinstance(data, dict):
            return False, "scheduling_response_corrupt"
        enabled = data.get("enabled")
        if type(enabled) is not bool:
            return False, "scheduling_response_corrupt"
        try:
            server_time = parse_aware_iso(data.get("server_time"))
            updated_raw = data.get("updated_at")
            updated_at = None if updated_raw is None else parse_aware_iso(updated_raw)
        except Exception:
            return False, "scheduling_response_corrupt"
        if abs(server_time - now) > self.max_skew_sec:
            return False, "scheduling_response_stale"
        if updated_at is not None and updated_at > server_time + 1.0:
            return False, "scheduling_response_corrupt"
        if self._last_server_time is not None and server_time < self._last_server_time - 1.0:
            return False, "scheduling_response_regressed"
        self._last_server_time = max(server_time, self._last_server_time or server_time)
        self._observed = (time.monotonic(), enabled)
        if self.cache is not None:
            try:
                self.cache.store({
                    "enabled": enabled,
                    "server_time": data["server_time"],
                    "updated_at": updated_raw,
                    "captured_at": iso_utc(now),
                    "origin": self._origin(),
                    "host_id": self.host_id,
                    "schema": 1,
                })
            except Exception:
                pass
        if not enabled:
            return False, "scheduling_disabled: hub"
        return True, "scheduling_enabled"

    def _origin(self) -> str:
        p = urllib.parse.urlparse(self.base_url)
        return f"{p.scheme}://{p.netloc}"


# ---------------------------------------------------------------------------
# Canonical incumbent inspection (B7). Read-only; no lock is ever taken.
# ---------------------------------------------------------------------------

def read_canonical_state_strict(state_dir: str | os.PathLike | None = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """(state, reason). state is None when absent ("absent") or unreadable ("unreadable"/"corrupt")."""
    base = _resolve(expand_trusted_path(state_dir or os.environ.get("HR_STATE") or DEFAULT_CANONICAL_STATE_DIR))
    path = base / "state.json"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, "absent"
    except OSError:
        return None, "unreadable"
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None, "corrupt"
    if not isinstance(data, dict):
        return None, "corrupt"
    return data, "ok"


def read_canonical_state(state_dir: str | os.PathLike | None = None) -> Dict[str, Any]:
    """Read-only inspection of the script-owned canonical root state.json ({} when absent/corrupt)."""
    data, _ = read_canonical_state_strict(state_dir)
    return data or {}


# Fixed inode of the initial PID namespace (kernel PROC_PID_INIT_INO); any other namespace id is nested.
INIT_PID_NAMESPACE = "pid:[4026531836]"


def in_init_pid_namespace(status_path: str = "/proc/self/status", ns_path: str = "/proc/self/ns/pid") -> Optional[bool]:
    """True only for a process in the initial (host) PID namespace that also reads the host's /proc
    (``NSpid`` has a single entry); False in any nested one; None when that cannot be read.

    ``NSpid`` alone is not enough: a procfs mounted inside a private namespace shows one entry too."""
    try:
        ns = os.readlink(ns_path)
        with open(status_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    if ns != INIT_PID_NAMESPACE:
        return False
    for line in lines:
        if line.startswith("NSpid:"):
            pids = line.split(":", 1)[1].split()
            if not pids or not all(p.isdigit() for p in pids):
                return None
            return len(pids) == 1
    return None


def root_lock_held(lock_path: pathlib.Path) -> Optional[bool]:
    """Whether root.lock is flock-held, without ever taking the lock. None = unknown.

    ``/proc/locks`` omits a lock whose owner pid cannot be translated into the reader's PID
    namespace (the real script's lock helper process exits at once, so the recorded owner is
    dead). Outside the initial PID namespace "not listed" therefore does not mean "free": only an
    init-namespace reader may answer, everywhere else the answer is unknown (HOLD).
    """
    try:
        st = os.stat(lock_path)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    if in_init_pid_namespace() is not True:
        return None
    try:
        with open("/proc/locks", "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    want = (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
    for line in lines:
        parts = line.split()
        if len(parts) < 6 or parts[1] not in ("FLOCK", "POSIX", "OFDLCK"):
            continue
        try:
            maj, minor, ino = parts[5].split(":")
            if (int(maj, 16), int(minor, 16), int(ino)) == want:
                return True
        except ValueError:
            continue
    return False


def read_process_identity(pid: int) -> Optional[Dict[str, Any]]:
    """Live /proc identity of a pid: pid namespace, start ticks, cgroup, uid."""
    try:
        return {
            "namespace": os.readlink(f"/proc/{pid}/ns/pid"),
            "start_ticks": get_pid_starttime_ticks(pid),
            "cgroup": pathlib.Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8"),
            "uid": os.stat(f"/proc/{pid}").st_uid,
        }
    except Exception:
        return None


def _int_field(value: Any) -> Optional[int]:
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    return None


def canonical_incumbent_status(
    state_dir: str | os.PathLike | None = None,
    clock: Any = None,
    lock_probe: Optional[Callable[[pathlib.Path], Optional[bool]]] = None,
    proc_probe: Optional[Callable[[int], Optional[Dict[str, Any]]]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Read-only incumbent classification matching the script's rules.

    Returns ("none"|"healthy"|"starting"|"stale"|"crashed"|"unknown", state).
    ``unknown`` (corrupt/unreadable state, unknown status, ambiguous or foreign
    live identity, unreadable lock state) must HOLD; it is never "no incumbent".
    ``starting`` is: lock held while the recorded status is not ``running``.
    """
    base = _resolve(expand_trusted_path(state_dir or os.environ.get("HR_STATE") or DEFAULT_CANONICAL_STATE_DIR))
    state, why = read_canonical_state_strict(base)
    held = (lock_probe or root_lock_held)(base / "root.lock")
    if state is None and why != "absent":
        return "unknown", {}
    if state is None:
        if held is None:
            return "unknown", {}
        return ("starting", {}) if held else ("none", {})
    if not state:
        return "unknown", state
    pst = state.get("status")
    if pst not in ("running", "finished", "superseded", "crashed"):
        return "unknown", state
    if held is None:
        return "unknown", state
    if pst != "running":
        return ("starting", state) if held else ("none", state)

    now_s = int(_clock_time(clock or time))
    pid = _int_field(state.get("pid", 0)) or 0
    hb = _int_field(state.get("heartbeat", state.get("hb", 0))) or 0
    st = _int_field(state.get("start", state.get("st", 0))) or 0
    if hb > now_s + 60 or st > now_s + 60:
        return "stale", state
    ns = state.get("pid_namespace")
    if ns is not None and ns != get_current_pid_namespace():
        return "unknown", state  # a pid from another namespace means nothing here
    if pid > 0:
        ident = (proc_probe or read_process_identity)(pid)
        if ident is None:
            if is_pid_alive(pid):
                return "unknown", state  # alive but its identity cannot be read: ambiguous
            return ("stale" if held else "crashed"), state
        ticks = _int_field(state.get("pid_start_ticks"))
        if ticks is not None and ident.get("start_ticks") != ticks:
            return ("stale" if held else "crashed"), state  # pid reused by an unrelated process
    hb_age = now_s - hb
    age = now_s - st
    if 0 <= hb_age < HB_STALE_SEC and 0 <= age < WINDOW_SEC + GRACE_SEC:
        return "healthy", state  # conservative: also when the lock reads free
    return "stale", state


# ---------------------------------------------------------------------------
# Launcher boundary and executor (B3)
# ---------------------------------------------------------------------------

def inspect_launcher_exec_boundary(launcher_path: pathlib.Path) -> Dict[str, Any]:
    """Static (AST) look at ``launcher/task_units.py``: which explicit parameters does
    ``execute_transient_task_unit`` name? Text in docstrings/comments cannot count."""
    import ast

    out: Dict[str, Any] = {"present": False, "params": [], "missing": list(REQUIRED_EXEC_PARAMS) + ["job_env|job_env_file"],
                           "env_param": None}
    src = pathlib.Path(launcher_path) / "launcher" / "task_units.py"
    try:
        tree = ast.parse(src.read_text(encoding="utf-8"))
    except Exception:
        return out
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "execute_transient_task_unit":
            a = node.args
            names = [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs]
            out["present"] = True
            out["params"] = names
            missing = [p for p in REQUIRED_EXEC_PARAMS if p not in names]
            env_param = next((p for p in ENV_PARAM_CHOICES if p in names), None)
            if env_param is None:
                missing.append("job_env|job_env_file")
            out["missing"] = missing
            out["env_param"] = env_param
            return out
    return out


@dataclasses.dataclass
class LaunchRequest:
    task_id: str
    command_argv: List[str]
    memory_mb: int
    workspace: str
    tmpdir: str
    timeout_sec: float
    quse_json: Any
    provider: str
    job_env: Dict[str, str]
    profile: str = ROOT_PROFILE

    def __repr__(self) -> str:  # never print the private environment
        return (f"LaunchRequest(task_id={self.task_id!r}, argv={self.command_argv!r}, "
                f"workspace={self.workspace!r}, env_keys={sorted(self.job_env)})")


# One lock for every import of ``launcher.*`` and every sys.modules/sys.path swap around it
# (also taken by runtime._inspected_launcher), so threads never see each other's half-swapped state.
LAUNCHER_IMPORT_LOCK = threading.RLock()


def _module_in_checkout(module: Any, repo: str) -> bool:
    """Whether ``module`` was loaded from ``repo``; a cached module of another checkout must not answer."""
    path = getattr(module, "__file__", None)
    if not isinstance(path, str) or not path:
        return False
    root = os.path.realpath(repo)
    return os.path.realpath(path).startswith(root + os.sep)


def fetch_quse_default(launcher_repo: str | os.PathLike | None = None) -> Any:
    """Fresh ``quse`` evidence via the installed launcher's own fetcher."""
    repo = os.path.expanduser(str(launcher_repo or "~/git/agent-quota-launcher"))
    with LAUNCHER_IMPORT_LOCK:
        inserted = False
        if repo not in sys.path:
            sys.path.insert(0, repo)
            inserted = True
        try:
            from launcher import admission  # type: ignore
            if not _module_in_checkout(admission, repo):
                raise DispatchError("launcher_module_foreign_checkout: launcher.admission")
            fetch = admission.fetch_quse
        except DispatchError as e:
            raise DispatchError(f"quota_evidence_unavailable: {e}")
        except Exception as e:
            raise DispatchError(f"quota_evidence_unavailable: {type(e).__name__}: {e}")
        finally:
            if inserted:
                try:
                    sys.path.remove(repo)
                except ValueError:
                    pass
    try:
        return fetch()
    except Exception as e:
        raise DispatchError(f"quota_evidence_unavailable: {type(e).__name__}: {e}")


class LauncherExecutor:
    """Default executor: the installed launcher API, with everything or nothing.

    The private job environment, fresh ``quse`` evidence, provider, workspace,
    ``TMPDIR`` under ``<workspace>/.local/tmp``, resource/capacity admission and
    the Root profile are passed through. If the installed signature does not
    explicitly name every one of them, the executor raises: nothing is silently
    dropped and no Root unit is started.
    """

    def __init__(self, launcher_repo: str | os.PathLike | None = None):
        self.launcher_repo = os.path.expanduser(str(launcher_repo or "~/git/agent-quota-launcher"))

    def _load(self):
        with LAUNCHER_IMPORT_LOCK:
            inserted = False
            if self.launcher_repo not in sys.path:
                sys.path.insert(0, self.launcher_repo)
                inserted = True
            try:
                from launcher import task_units  # type: ignore
                if not _module_in_checkout(task_units, self.launcher_repo):
                    raise DispatchError("launcher_module_foreign_checkout: launcher.task_units")
                return task_units
            except DispatchError as e:
                raise DispatchError(f"launcher_runtime_unavailable: {e}")
            except Exception as e:
                raise DispatchError(f"launcher_runtime_unavailable: {type(e).__name__}: {e}")
            finally:
                if inserted:
                    try:
                        sys.path.remove(self.launcher_repo)
                    except ValueError:
                        pass

    def __call__(self, req: LaunchRequest) -> Dict[str, Any]:
        task_units = self._load()
        fn = getattr(task_units, "execute_transient_task_unit", None)
        if fn is None:
            raise DispatchError("launcher_runtime_incomplete: execute_transient_task_unit missing")
        params = inspect.signature(fn).parameters
        named = {n for n, p in params.items() if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
        missing = [p for p in REQUIRED_EXEC_PARAMS if p not in named]
        env_param = next((p for p in ENV_PARAM_CHOICES if p in named), None)
        if env_param is None:
            missing.append("job_env|job_env_file")
        if missing:
            raise DispatchError("launcher_root_boundary_unsupported: installed execute_transient_task_unit "
                                f"does not name: {', '.join(missing)}")
        kwargs: Dict[str, Any] = dict(
            task_id=req.task_id, command_argv=req.command_argv, memory_mb=req.memory_mb,
            workspace=req.workspace, tmpdir=req.tmpdir, timeout_sec=req.timeout_sec,
            quse_json=req.quse_json, provider=req.provider, check_capacity=True, profile=req.profile,
        )
        env_file: Optional[pathlib.Path] = None
        if env_param == "job_env":
            kwargs["job_env"] = dict(req.job_env)
        else:
            d = ensure_private_dir(pathlib.Path(req.tmpdir) / "hub-env")
            env_file = d / f"{req.task_id}.env"
            fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for k, v in sorted(req.job_env.items()):
                    f.write(f"{k}={v}\n")
            kwargs["job_env_file"] = str(env_file)
        try:
            receipt = fn(**kwargs)
        finally:
            if env_file is not None:
                try:
                    os.unlink(env_file)
                except OSError:
                    pass
        if not isinstance(receipt, dict):
            raise DispatchError("launcher_receipt_not_dict")
        return receipt


class SystemdUserUnitController:
    """Exact-unit control: signals only when the live InvocationID matches the owned one."""

    PROPS = ("InvocationID", "ActiveState", "LoadState", "ControlGroup")

    def show(self, unit: str) -> Dict[str, str]:
        cmd = ["systemctl", "--user", "show", unit]
        for p in self.PROPS:
            cmd += ["-p", p]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10.0, check=False)
        if res.returncode != 0:
            raise DispatchError(f"unit_state_unknown: systemctl show rc={res.returncode}")
        props: Dict[str, str] = {}
        for line in res.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v.strip()
        return props

    def signal(self, unit: str, expected_invocation: str, verb: str) -> bool:
        if verb not in ("stop", "kill"):
            raise ValueError("verb must be stop or kill")
        exp = (expected_invocation or "").strip()
        live = (self.show(unit).get("InvocationID") or "").strip()
        if not exp or not live or exp != live:
            return False
        cmd = ["systemctl", "--user", "stop", unit] if verb == "stop" else [
            "systemctl", "--user", "kill", "--signal=SIGKILL", unit]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60.0, check=False)
        return True


# ---------------------------------------------------------------------------
# Expected run, receipt validation (B2)
# ---------------------------------------------------------------------------

def _safe_token(text: str, prefix: str, max_len: int = 96) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", text)[:max_len].strip("-")
    return f"{prefix}{cleaned}" if cleaned else f"{prefix}{hashlib.sha256(text.encode()).hexdigest()[:16]}"


def unit_name_for_task(task_id: str) -> str:
    return f"agent-task-{task_id}.service"


@dataclasses.dataclass
class ExpectedRun:
    job_id: str
    host_id: str
    lease_epoch: int
    run_id: str
    task_id: str
    unit: str
    entry_sha256: str
    dispatched_at: float
    run_dir: pathlib.Path
    first_action_file: pathlib.Path
    start_ack_file: pathlib.Path


def _req_str(d: Dict[str, Any], key: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v.strip():
        raise DispatchError(f"receipt_field_invalid: {key}")
    return v


def _req_int(d: Dict[str, Any], key: str) -> int:
    v = d.get(key)
    if type(v) is not int or v <= 0:
        raise DispatchError(f"receipt_field_invalid: {key}")
    return v


def check_receipt_fields(receipt: Dict[str, Any], expected: ExpectedRun, now: float,
                         skew_sec: float = 5.0) -> Dict[str, Any]:
    """Static identity/time checks of a receipt against the host's own expectations."""
    if type(receipt.get("schema_version")) is not int or receipt["schema_version"] != RECEIPT_SCHEMA_VERSION:
        raise DispatchError("receipt_schema_unsupported")
    kind = receipt.get("kind")
    if kind not in RECEIPT_KINDS:
        raise DispatchError("receipt_kind_unknown")
    for key, want in (("job_id", expected.job_id), ("host_id", expected.host_id), ("run_id", expected.run_id),
                      ("launcher_task_id", expected.task_id), ("launcher_unit", expected.unit)):
        if _req_str(receipt, key) != want:
            raise DispatchError(f"receipt_mismatch: {key}")
    if _req_int(receipt, "hub_lease_epoch") != expected.lease_epoch:
        raise DispatchError("receipt_mismatch: hub_lease_epoch")
    native_epoch = _req_int(receipt, "native_epoch")
    invocation = _req_str(receipt, "launcher_invocation_id")
    action = _req_str(receipt, "actual_action")
    if len(action) > 512:
        raise DispatchError("receipt_field_invalid: actual_action")
    if not _SHA256_RE.match(_req_str(receipt, "actual_action_digest")):
        raise DispatchError("receipt_field_invalid: actual_action_digest")
    if _req_str(receipt, "source_entrypoint_sha256") != expected.entry_sha256:
        raise DispatchError("receipt_mismatch: source_entrypoint_sha256")
    session = receipt.get("native_session_id")
    if kind == "observed_native_tool":
        if not isinstance(session, str) or not session.strip():
            raise DispatchError("receipt_native_session_missing")
    elif session not in (None, ""):
        raise DispatchError("receipt_bootstrap_claims_native_session")
    try:
        observed = parse_aware_iso(receipt.get("observed_at_utc"))
    except Exception:
        raise DispatchError("receipt_field_invalid: observed_at_utc")
    if observed < expected.dispatched_at - skew_sec or observed > now + skew_sec:
        raise DispatchError("receipt_time_outside_dispatch_window")
    return {"kind": kind, "native_epoch": native_epoch, "launcher_invocation_id": invocation,
            "native_session_id": session if kind == "observed_native_tool" else None, "actual_action": action}


def validate_run_receipt(
    path: pathlib.Path,
    expected: ExpectedRun,
    canonical_state_dir: pathlib.Path,
    now: float,
    proc_probe: Callable[[int], Optional[Dict[str, Any]]] = read_process_identity,
    invocation_probe: Optional[Callable[[str], Optional[str]]] = None,
    host_namespace: Optional[str] = None,
    skew_sec: float = 5.0,
) -> Dict[str, Any]:
    """Validate a receipt produced by THIS run. Raises DispatchError otherwise.

    Binds the receipt to the host's own expectations, to the script-owned
    ``state.json`` and to live process facts (pid namespace, start ticks, unit
    cgroup, systemd InvocationID). Never derives anything from state or PIDs
    alone.
    """
    try:
        receipt = read_private_json(path)
    except FileNotFoundError:
        raise DispatchError("receipt_missing")
    fields = check_receipt_fields(receipt, expected, now, skew_sec)
    if os.lstat(path).st_mtime < expected.dispatched_at - skew_sec:
        raise DispatchError("receipt_file_predates_dispatch")
    invocation = fields["launcher_invocation_id"]

    state, why = read_canonical_state_strict(canonical_state_dir)
    if state is None:
        raise DispatchError(f"receipt_state_unavailable: {why}")
    if state.get("run_id") != expected.run_id or state.get("status") != "running":
        raise DispatchError("receipt_state_not_this_run")
    for key, want in (("hub_job_id", expected.job_id), ("launcher_task_id", expected.task_id),
                      ("launcher_unit", expected.unit), ("launcher_invocation_id", invocation)):
        if state.get(key) != want:
            raise DispatchError(f"receipt_state_mismatch: {key}")
    if _int_field(state.get("hub_lease_epoch")) != expected.lease_epoch:
        raise DispatchError("receipt_state_mismatch: hub_lease_epoch")
    if _int_field(state.get("epoch")) != fields["native_epoch"]:
        raise DispatchError("receipt_state_mismatch: epoch")
    start = _int_field(state.get("start"))
    if start is None or start < int(expected.dispatched_at) - 2:
        raise DispatchError("receipt_state_predates_dispatch")
    pid = _int_field(state.get("pid"))
    if pid is None or pid <= 1 or pid in (os.getpid(), os.getppid(), 65534):
        raise DispatchError("receipt_state_pid_invalid")
    host_ns = host_namespace if host_namespace is not None else get_current_pid_namespace()
    if not host_ns or state.get("pid_namespace") != host_ns:
        raise DispatchError("receipt_foreign_pid_namespace")
    ident = proc_probe(pid)
    if ident is None:
        raise DispatchError("receipt_process_not_live")
    ticks = _int_field(state.get("pid_start_ticks"))
    if ticks is None or ident.get("start_ticks") != ticks:
        raise DispatchError("receipt_process_identity_mismatch: start_ticks")
    if ident.get("namespace") != host_ns:
        raise DispatchError("receipt_foreign_pid_namespace")
    if expected.unit not in str(ident.get("cgroup", "")):
        raise DispatchError("receipt_process_not_in_owned_unit")
    if invocation_probe is not None:
        live_inv = invocation_probe(expected.unit)
        if not live_inv or live_inv != invocation:
            raise DispatchError("receipt_invocation_mismatch")
    return {**fields, "path": str(path), "digest": file_sha256(path), "pid": pid}


# ---------------------------------------------------------------------------
# RootRun session and adapter
# ---------------------------------------------------------------------------

RUN_STATUSES = ("ok", "skipped", "quota_held", "timeout", "error")
_RUN_TO_HUB = {"ok": "succeeded", "skipped": "cancelled", "quota_held": "cancelled",
               "timeout": "failed", "error": "failed"}


class RootRun:
    """One Root cycle: launcher unit thread + receipt/ack/outcome handling."""

    def __init__(self, adapter: "RootDispatchAdapter", expected: ExpectedRun, request: LaunchRequest,
                 job_token: str, fixture_only: bool):
        self.adapter = adapter
        self.expected = expected
        self.request = request
        self._job_token = job_token
        self.fixture_only = fixture_only
        self._receipt: Optional[Dict[str, Any]] = None
        self._exc: Optional[BaseException] = None
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"root-run-{expected.run_id}", daemon=True)
        self._owned_invocation: Optional[str] = None
        self._bootstrap: Optional[Dict[str, Any]] = None
        self._acked = False
        self._cancelled: Optional[Dict[str, Any]] = None
        self.started_epoch = time.time()

    # -- lifecycle -----------------------------------------------------
    def _run(self) -> None:
        try:
            self._receipt = self.adapter.executor(self.request)
        except BaseException as e:  # noqa: BLE001 - recorded, never hidden
            self._exc = e
        finally:
            self._done.set()

    def launch(self) -> None:
        self._thread.start()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def thread_alive(self) -> bool:
        return self._thread.is_alive()

    def _exec_error_text(self) -> str:
        return scrub(f"{type(self._exc).__name__}: {self._exc}", self._job_token)

    # -- bootstrap receipt ----------------------------------------------
    def wait_bootstrap(self, timeout: float = BOOTSTRAP_TIMEOUT_SEC) -> Optional[Dict[str, Any]]:
        """Validated first-action receipt of this run, or None when the unit ended without
        one (see ``outcome()``). Raises DispatchError on invalid/mismatched evidence,
        executor failure or timeout."""
        deadline = time.monotonic() + timeout
        last_err: Optional[DispatchError] = None
        first = self.expected.first_action_file
        while True:
            if self._exc is not None:
                raise DispatchError(f"launcher_execution_failed: {self._exec_error_text()}")
            ended = self._done.is_set()
            if os.path.lexists(first):
                try:
                    ev = validate_run_receipt(
                        first, self.expected, self.adapter.config.canonical_state_dir,
                        _clock_time(self.adapter.clock), self.adapter.proc_probe,
                        self.adapter.invocation_probe, self.adapter.host_namespace)
                except DispatchError as e:
                    if not (str(e) == "receipt_missing" or str(e).startswith("file_corrupt_json")):
                        raise
                    last_err = e  # a vanished/partial file: keep polling until timeout
                else:
                    self._owned_invocation = ev["launcher_invocation_id"]
                    self._bootstrap = ev
                    return ev
            elif ended:
                return None
            if ended:
                raise DispatchError(f"no_first_action_receipt: {last_err}")
            if time.monotonic() >= deadline:
                raise DispatchError(f"no_first_action_receipt: {last_err or 'timeout'}")
            time.sleep(0.05)

    def acknowledge_start(self, hub_response: Dict[str, Any]) -> str:
        """Write the start acknowledgement the script waits for. Requires a validated
        bootstrap receipt and a Hub ``start`` answer without error."""
        if self._bootstrap is None:
            raise DispatchError("ack_without_validated_receipt")
        if not isinstance(hub_response, dict) or hub_response.get("error"):
            raise DispatchError("ack_without_hub_start")
        ex = self.expected
        write_private_json(ex.start_ack_file, {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "job_id": ex.job_id, "host_id": ex.host_id, "hub_lease_epoch": ex.lease_epoch,
            "native_epoch": self._bootstrap["native_epoch"], "run_id": ex.run_id,
            "launcher_task_id": ex.task_id, "launcher_unit": ex.unit,
            "launcher_invocation_id": self._bootstrap["launcher_invocation_id"],
            "first_action_digest": self._bootstrap["digest"],
            "acknowledged_at_utc": iso_utc(_clock_time(self.adapter.clock)),
        })
        self._acked = True
        return str(ex.start_ack_file)

    # -- waiting / outcome -----------------------------------------------
    def wait_done(self, timeout: float) -> bool:
        return self._done.wait(timeout)

    def outcome(self) -> Dict[str, Any]:
        """Hub-facing outcome derived from this run's own summary, state and unit receipt."""
        ex = self.expected
        refs: List[str] = []
        if self._exc is not None:
            return {"hub_status": "failed", "run_status": "error",
                    "summary": f"launcher_execution_failed: {self._exec_error_text()}", "evidence_refs": refs}
        receipt = self._receipt or {}
        unit = receipt.get("unit_name")
        exit_code = receipt.get("exit_code")
        if unit != ex.unit or type(exit_code) is not int:
            return {"hub_status": "failed", "run_status": "error",
                    "summary": "launcher_receipt_missing_actual_fields", "evidence_refs": refs}
        if not self.fixture_only and (receipt.get("cleanup_verified") is not True
                or not isinstance(receipt.get("invocation_id"), str)
                or re.fullmatch(r"[0-9a-fA-F]{32}", receipt["invocation_id"]) is None):
            return {"hub_status": "failed", "run_status": "error",
                    "summary": "launcher_receipt_cleanup_or_invocation_unverified", "evidence_refs": refs}
        if self._owned_invocation and receipt.get("invocation_id") not in (None, "", self._owned_invocation):
            return {"hub_status": "failed", "run_status": "error",
                    "summary": "launcher_receipt_invocation_mismatch", "evidence_refs": refs}
        runs = self.adapter.config.runs_dir
        summary_path = runs / f"{ex.run_id}.json"
        try:
            summary = read_private_json(summary_path, 1 << 20)
        except FileNotFoundError:
            return {"hub_status": "failed", "run_status": "crashed",
                    "summary": f"crashed: no run summary for {ex.run_id} (unit exit {exit_code})",
                    "evidence_refs": refs}
        except DispatchError as e:
            return {"hub_status": "failed", "run_status": "error", "summary": f"run_summary_invalid: {e}",
                    "evidence_refs": refs}
        refs.append(str(summary_path))
        handoff = runs / f"{ex.run_id}.handoff.md"
        if handoff.is_file():
            refs.append(str(handoff))
        status = summary.get("status")
        if summary.get("run_id") != ex.run_id or status not in RUN_STATUSES:
            return {"hub_status": "failed", "run_status": "error",
                    "summary": "run_summary_mismatch", "evidence_refs": refs}
        state, _ = read_canonical_state_strict(self.adapter.config.canonical_state_dir)
        if state is not None and state.get("run_id") not in (None, ex.run_id) and self._bootstrap is not None:
            return {"hub_status": "failed", "run_status": "fenced",
                    "summary": f"fenced: canonical state now owned by {state.get('run_id')}", "evidence_refs": refs}
        if self._bootstrap is not None and state is not None and state.get("run_id") == ex.run_id \
                and state.get("status") == "running":
            return {"hub_status": "failed", "run_status": "crashed",
                    "summary": f"crashed: run {ex.run_id} left status running after unit exit {exit_code}",
                    "evidence_refs": refs}
        hub_status = _RUN_TO_HUB[status]
        if self._bootstrap is not None and _int_field(summary.get("epoch")) not in (None, self._bootstrap["native_epoch"]):
            return {"hub_status": "failed", "run_status": "error",
                    "summary": "run_summary_mismatch: epoch", "evidence_refs": refs}
        if status == "ok":
            if self._bootstrap is None or not self._acked:
                return {"hub_status": "failed", "run_status": "error",
                        "summary": "run reported ok without a validated receipt and Hub start acknowledgement",
                        "evidence_refs": refs}
            if exit_code != 0 or summary.get("exit_code") != 0:
                return {"hub_status": "failed", "run_status": "error",
                        "summary": f"run reported ok but exit code {exit_code}/{summary.get('exit_code')}",
                        "evidence_refs": refs}
        native = self._native_tool_note()
        text = {"ok": "completed", "skipped": "skipped (previous root healthy; no Root cycle ran)",
                "quota_held": "skipped (quota gate held; no Root cycle ran)",
                "timeout": "timeout", "error": "error"}[status]
        refs_out = refs + ([str(ex.first_action_file)] if self._bootstrap is not None else [])
        return {"hub_status": hub_status, "run_status": status, "exit_code": exit_code,
                "summary": f"root run {ex.run_id}: {text}; {native}", "evidence_refs": refs_out,
                "launcher_unit": unit, "launcher_invocation_id": receipt.get("invocation_id")}

    def _native_tool_note(self) -> str:
        """Native model first tool: only a receipt of that kind counts; otherwise UNKNOWN."""
        try:
            receipt = read_private_json(self.expected.first_action_file)
            fields = check_receipt_fields(receipt, self.expected, time.time())
        except Exception:
            fields = None
        if fields and fields["kind"] == "observed_native_tool":
            return (f"native first tool observed (session {fields['native_session_id']}, "
                    f"digest {file_sha256(self.expected.first_action_file)[:12]})")
        return "native first tool: UNKNOWN (bootstrap receipt only)"

    # -- cancellation (B5) -----------------------------------------------
    @property
    def cleanup_unverified(self) -> bool:
        """An invalid or foreign launcher receipt cannot prove the owned unit was cleaned up.
        Stop and live-check that exact unit before reporting an outcome."""
        if self._exc is not None:
            return True
        receipt = self._receipt
        if not isinstance(receipt, dict):
            return True
        if receipt.get("unit_name") != self.expected.unit or type(receipt.get("exit_code")) is not int:
            return True
        invocation = receipt.get("invocation_id")
        if self._owned_invocation and invocation != self._owned_invocation:
            return True
        if self.fixture_only:
            return False
        return (receipt.get("cleanup_verified") is not True
                or not isinstance(invocation, str)
                or re.fullmatch(r"[0-9a-fA-F]{32}", invocation) is None)

    def cleanup_after_exit(self, wait_sec: float = 30.0, attempts: int = 2) -> Optional[Dict[str, Any]]:
        """After the launcher thread ended: cancel (and live-verify) the exact owned unit unless the
        launcher itself verified its cleanup. None when nothing needed doing."""
        if not self.cleanup_unverified:
            return None
        return self.cancel("launcher_cleanup_unverified", wait_sec=wait_sec, attempts=attempts)

    def _unit_gone(self, props: Dict[str, str]) -> Optional[str]:
        """Why the owned invocation is gone according to a live ``systemctl show``, else None."""
        if props.get("LoadState") == "not-found":
            return "not-found"
        if props.get("ActiveState") in ("inactive", "failed"):
            return "inactive"
        live = (props.get("InvocationID") or "").strip()
        if self._owned_invocation and live and live != self._owned_invocation:
            return "invocation_changed"
        return None

    def _settle_owned_unit(self, info: Dict[str, Any], wait_sec: float) -> Optional[str]:
        """Stop, then kill, ONLY the exact owned invocation until a live unit query shows it gone.

        Returns the live reason (``not-found``/``inactive``/``invocation_changed``) once the launcher
        thread has ended and the unit is gone, else None. A changed or foreign invocation is never
        signalled successfully (the controller refuses unless the live id equals the owned id)."""
        ex = self.expected
        ctl = self.adapter.unit_controller
        end = time.monotonic() + wait_sec
        grace = min(15.0, wait_sec / 2)
        stopped_at: Optional[float] = None
        killed = False
        while True:
            thread_ended = not self._thread.is_alive()
            props = ctl.show(ex.unit)
            gone = self._unit_gone(props)
            info["unit_state"] = gone or "live"
            if gone is not None and thread_ended:
                return gone
            inv = self._owned_invocation
            if inv is None:
                live = (props.get("InvocationID") or "").strip()
                if gone is None and live and props.get("ActiveState") in ("active", "activating") \
                        and ex.unit in props.get("ControlGroup", ""):
                    self._owned_invocation = live
                    continue
            elif gone in (None, "invocation_changed") and stopped_at is None:
                info["signalled"] = bool(ctl.signal(ex.unit, inv, "stop")) or info["signalled"]
                stopped_at = time.monotonic()
            elif gone is None and not killed and stopped_at is not None and time.monotonic() - stopped_at >= grace:
                info["signalled_kill"] = bool(ctl.signal(ex.unit, inv, "kill"))
                killed = True
            if time.monotonic() >= end:
                return None
            time.sleep(0.1)

    def cancel(self, reason: str, wait_sec: float = 30.0, attempts: int = 2) -> Dict[str, Any]:
        """Stop and wait ONLY the exact owned unit/invocation. Never signals anything else.

        ``verified`` needs BOTH the launcher thread ended AND a live ``systemctl show`` reporting the
        unit not-found/inactive or its InvocationID changed; thread exit alone never counts (the
        launcher can raise with the unit still active). Retries a bounded number of times; an
        unverified result is returned honestly so the Hub is told the unit may still be running."""
        if self._cancelled is not None:
            return self._cancelled
        ex = self.expected
        info: Dict[str, Any] = {"reason": scrub(reason, self._job_token), "unit": ex.unit, "signalled": False}
        gone: Optional[str] = None
        for _ in range(max(1, attempts)):
            try:
                gone = self._settle_owned_unit(info, wait_sec)
                info.pop("error", None)
            except Exception as e:  # noqa: BLE001 - reported, never hidden
                gone = None
                info["error"] = scrub(f"{type(e).__name__}: {e}", self._job_token)
            if gone is not None:
                break
        info["thread_alive"] = self._thread.is_alive()
        info["verified"] = gone is not None and not info["thread_alive"]
        if info["verified"]:
            self._cancelled = info
        return info


class RootDispatchAdapter:
    """Prepares and launches one bounded root run through the launcher task-unit API."""

    def __init__(
        self,
        script_path: str | os.PathLike | None = None,
        allowed_repo_root: str | os.PathLike | None = None,
        state_dir: Optional[str | os.PathLike] = None,
        runs_dir: Optional[str | os.PathLike] = None,
        launcher_executor: Optional[Callable[[LaunchRequest], Dict[str, Any]]] = None,
        clock: Optional[Any] = None,
        memory_mb: int = ROOT_MEMORY_MB,
        timeout_sec: float = WINDOW_SEC,
        config: Optional[TrustedRootConfig] = None,
        host_state_dir: Optional[str | os.PathLike] = None,
        scheduling_check: Optional[Callable[..., Tuple[bool, str]]] = None,
        quota_check: Optional[Callable[[], Tuple[bool, str]]] = None,
        quse_provider: Optional[Callable[[], Any]] = None,
        unit_controller: Optional[Any] = None,
        proc_probe: Optional[Callable[[int], Optional[Dict[str, Any]]]] = None,
        invocation_probe: Optional[Callable[[str], Optional[str]]] = None,
        host_namespace: Optional[str] = None,
        launcher_repo: Optional[str] = None,
    ):
        self.config = config or TrustedRootConfig.build(script_path, allowed_repo_root, state_dir, host_state_dir)
        if runs_dir is not None and _resolve(runs_dir) != self.config.runs_dir:
            raise DispatchError("runs_dir_must_be_under_canonical_state_dir")
        # The injected executor is a test seam: every receipt it yields is marked fixture-only.
        self._fixture_executor = launcher_executor
        # So is every injected probe/controller: production passes none of these.
        self._fixture_seams = tuple(sorted(name for name, value in (
            ("launcher_executor", launcher_executor), ("proc_probe", proc_probe),
            ("invocation_probe", invocation_probe), ("host_namespace", host_namespace),
            ("unit_controller", unit_controller), ("quse_provider", quse_provider),
            ("clock", clock)) if value is not None))
        self.executor = launcher_executor or LauncherExecutor(launcher_repo)
        self.clock = clock or time
        self.memory_mb = memory_mb
        self.timeout_sec = timeout_sec
        self.scheduling_check = scheduling_check
        self.quota_check = quota_check
        self.quse_provider = quse_provider or (lambda: fetch_quse_default(launcher_repo))
        self.unit_controller = unit_controller or SystemdUserUnitController()
        self.proc_probe = proc_probe or read_process_identity
        self.invocation_probe = invocation_probe if invocation_probe is not None else self._live_invocation
        self.host_namespace = host_namespace if host_namespace is not None else get_current_pid_namespace()

    @property
    def is_fixture_executor(self) -> bool:
        return bool(self._fixture_seams)

    def _live_invocation(self, unit: str) -> Optional[str]:
        try:
            return (self.unit_controller.show(unit).get("InvocationID") or "").strip() or None
        except Exception:
            return None

    # -- begin ----------------------------------------------------------
    def begin(
        self,
        job_id: str,
        job_token: str,
        lease_epoch: int,
        hub_url: str,
        host_id: str,
        workspace: Optional[str] = None,
    ) -> RootRun:
        """Refuse-or-launch. Every check runs before any file or process effect."""
        if not isinstance(job_id, str) or not job_id.strip():
            raise DispatchError("job_id_invalid")
        if type(lease_epoch) is not int or lease_epoch <= 0:
            raise DispatchError("lease_epoch_invalid")
        if not isinstance(job_token, str) or not job_token.strip():
            raise DispatchError("job_token_missing")
        if not isinstance(host_id, str) or not host_id.strip():
            raise DispatchError("host_id_missing")

        # 1. Live scheduling read for this new run (never a local file).
        if self.scheduling_check is None:
            raise DispatchError("scheduling_reader_unbound")
        allowed, reason = self.scheduling_check()
        if not allowed:
            raise DispatchError(reason)

        # 2. Strict trusted entrypoint, hashed for receipt binding.
        entry = self.config.validated_entrypoint()
        entry_sha = file_sha256(entry)

        # 3. Fresh quota gate before the launch.
        if self.quota_check is not None:
            ok, why = self.quota_check()
            if not ok:
                raise DispatchError(f"quota_gate_refused: {why}")

        # 4. Incumbent: only none/stale/crashed may proceed; unknown HOLDS.
        cst, _ = canonical_incumbent_status(self.config.canonical_state_dir, self.clock)
        if cst in ("healthy", "starting"):
            raise DispatchError("skip_healthy_incumbent")
        if cst == "unknown":
            raise DispatchError("root_state_unknown")

        # 5. Pre-assigned identities; nothing may pre-exist for them.
        run_id = _safe_token(job_id, "hub-", 80) + f"-e{lease_epoch}"
        if not _SAFE_ID_RE.match(run_id):
            raise DispatchError("run_id_unsafe")
        task_id = _safe_token(job_id, "hub-root-", 80) + f"-e{lease_epoch}"
        unit = unit_name_for_task(task_id)
        runs = self.config.runs_dir
        if (runs / f"{run_id}.json").exists() or (runs / f"{run_id}.hub").exists():
            raise DispatchError("pre_existing_run_artifacts")
        state, _ = read_canonical_state_strict(self.config.canonical_state_dir)
        if state and state.get("run_id") == run_id:
            raise DispatchError("pre_existing_run_artifacts")
        try:
            props = self.unit_controller.show(unit)
        except DispatchError:
            raise
        except Exception as e:
            raise DispatchError(f"unit_state_unknown: {type(e).__name__}")
        if props.get("LoadState") != "not-found" and props.get("ActiveState") not in ("inactive", "failed", None, ""):
            raise DispatchError("unit_name_in_use")

        # 6. Fresh quota evidence for launcher admission.
        quse = self.quse_provider()
        if quse is None:
            raise DispatchError("quota_evidence_unavailable")

        workspace_dir = pathlib.Path(workspace) if workspace else self.config.repo_root
        tmpdir = workspace_dir / ".local" / "tmp" / task_id
        run_dir = runs / f"{run_id}.hub"
        runs.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(mode=0o700)
        dispatched_at = _clock_time(self.clock)
        expected = ExpectedRun(
            job_id=job_id, host_id=host_id, lease_epoch=lease_epoch, run_id=run_id, task_id=task_id, unit=unit,
            entry_sha256=entry_sha, dispatched_at=dispatched_at, run_dir=run_dir,
            first_action_file=run_dir / "first-action.json", start_ack_file=run_dir / "start-ack.json",
        )
        job_env = {
            "HUB_JOB_ID": job_id,
            "HUB_URL": hub_url,
            "HUB_LEASE_EPOCH": str(lease_epoch),
            "HUB_JOB_TOKEN": job_token,
            "HUB_HOST_ID": host_id,
            "HUB_LAUNCHER_TASK_ID": task_id,
            "HUB_LAUNCHER_UNIT": unit,
            "HUB_EXPECTED_RUN_ID": run_id,
            "HUB_FIRST_ACTION_FILE": str(expected.first_action_file),
            "HUB_START_ACK_FILE": str(expected.start_ack_file),
            "HR_STATE": str(self.config.canonical_state_dir),
        }
        request = LaunchRequest(
            task_id=task_id, command_argv=["bash", str(entry)], memory_mb=self.memory_mb,
            workspace=str(workspace_dir), tmpdir=str(tmpdir), timeout_sec=self.timeout_sec,
            quse_json=quse, provider=ROOT_PROVIDER, job_env=job_env,
        )
        run = RootRun(self, expected, request, job_token, fixture_only=self.is_fixture_executor)
        run.launch()
        return run

    def dispatch(
        self,
        job_id: str,
        job_token: str,
        lease_epoch: int,
        hub_url: str,
        payload: Dict[str, Any],
        workspace: Optional[str] = None,
        host_id: str = "",
        start_callback: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Blocking convenience over ``begin``. Without ``start_callback`` (the Hub
        start) the script's business step would never be acknowledged, so refuse."""
        if start_callback is None:
            raise DispatchError("start_callback_required")
        run = self.begin(job_id, job_token, lease_epoch, hub_url, host_id or "host", workspace)
        try:
            ev = run.wait_bootstrap()
            if ev is not None:
                run.acknowledge_start(start_callback(ev))
            if not run.wait_done(self.timeout_sec + 30.0):
                raise DispatchError("run_timeout")
            cleanup = run.cleanup_after_exit()
            out = run.outcome()
            if cleanup is not None:
                out["summary"] = f"{out['summary']}; cancel_verified={cleanup.get('verified')}"
        except BaseException:
            run.cancel("dispatch_failed")
            raise
        out["fixture_only"] = run.fixture_only
        out["status"] = out["hub_status"]
        return out
