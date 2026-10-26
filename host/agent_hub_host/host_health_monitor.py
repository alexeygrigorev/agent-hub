"""Host resource health monitor for Hetzner and Linux worker nodes.

Provides:
- Native Linux stdlib load averages (os.getloadavg 1m, 5m, 15m)
- Native Linux /proc/meminfo memory statistics (MemTotal, MemAvailable, used bytes, usage percent)
- Memory pressure evaluation (>=85% warn, >=95% critical)
- Pure standard library implementation (no psutil or external dependencies)
- Rate-limited, configuration-driven collector hook for session_host observer threads
"""

from __future__ import annotations

import errno
import json
import os
import re
import selectors
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import api, auth, config

CONFIG_FILENAME = "host-health.json"
DEFAULT_MIN_INTERVAL_S = 60
MIN_INTERVAL_FLOOR_S = 60
MAX_INTERVAL_S = 3600
DEFAULT_WARN_PERCENT = 85.0
DEFAULT_CRITICAL_PERCENT = 95.0
MAX_PAYLOAD_BYTES = 32 * 1024

DEFAULT_BUDGET_CAP_USD = Decimal("5.00")
DEFAULT_STORAGE_SOFT_BYTES = 128 * 1024 * 1024  # 128 MiB
DEFAULT_STORAGE_HARD_BYTES = 256 * 1024 * 1024  # 256 MiB
MAX_STORAGE_SCAN_ENTRIES = 5000

DEFAULT_HETZNER_SERVICES: Tuple[Dict[str, Any], ...] = (
    {
        "service_id": "supervision",
        "workspace": "/home/alexey/git/cloudflare-agent-git",
        "session_tag": "experiment-supervision",
        "status_path": "/home/alexey/git/cloudflare-agent-git/.local/supervision/status.json",
    },
)

DEFAULT_HETZNER_MACHINE_IDS = frozenset({"hetzner", "DEFLf4-rahKf9wE4TQAZfA"})
SUPERVISOR_STALE_THRESHOLD_S = 600.0
MAX_STATUS_FILE_BYTES = 256 * 1024
MAX_APLEXER_OUTPUT_BYTES = 64 * 1024
DECIMAL_STRING_PATTERN = re.compile(r"^\d{1,14}(\.\d{1,18})?$")

_ALLOWED_CONFIG_KEYS = frozenset({
    "enabled",
    "machine_id",
    "min_interval_s",
    "warn_threshold_percent",
    "critical_threshold_percent",
    "endpoint",
    "services",
    "runtime_services",
    "runtime_cooldown_s",
    "budget",
    "storage_paths",
    "storage_soft_bytes",
    "storage_hard_bytes",
})


def resolve_config_path(explicit: str | None = None) -> str:
    """Resolve trusted host health config path. Explicit wins."""
    if explicit:
        return explicit
    env = os.environ.get("HUB_HOST_HEALTH_CONFIG")
    if env:
        return env
    cwd = os.getcwd()
    for candidate in (
        os.path.join(cwd, ".local", "session", CONFIG_FILENAME),
        os.path.join(cwd, "journal", CONFIG_FILENAME),
    ):
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(cwd, ".local", "session", CONFIG_FILENAME)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def collect_load_averages(
    getloadavg_fn: Optional[Callable[[], Tuple[float, float, float]]] = None,
) -> Dict[str, float]:
    """Collect 1m, 5m, 15m load averages using stdlib os.getloadavg."""
    fn = getloadavg_fn or (getattr(os, "getloadavg", None))
    if not fn:
        return {"load1": 0.0, "load5": 0.0, "load15": 0.0}
    try:
        l1, l5, l15 = fn()
        return {
            "load1": round(float(l1), 2),
            "load5": round(float(l5), 2),
            "load15": round(float(l15), 2),
        }
    except Exception:
        return {"load1": 0.0, "load5": 0.0, "load15": 0.0}


def collect_memory_stats(
    meminfo_path: str = "/proc/meminfo",
    *,
    warn_percent: float = DEFAULT_WARN_PERCENT,
    critical_percent: float = DEFAULT_CRITICAL_PERCENT,
    open_fn: Callable[..., Any] = open,
) -> Dict[str, Any]:
    """Collect memory statistics from Linux /proc/meminfo stdlib file."""
    total_kb: Optional[int] = None
    available_kb: Optional[int] = None
    free_kb: Optional[int] = None
    buffers_kb: Optional[int] = None
    cached_kb: Optional[int] = None

    try:
        with open_fn(meminfo_path, "r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split(":", 1)
                if len(parts) != 2:
                    continue
                key = parts[0].strip()
                val_parts = parts[1].strip().split()
                if not val_parts:
                    continue
                try:
                    val_kb = int(val_parts[0])
                except (ValueError, TypeError):
                    continue

                if key == "MemTotal":
                    total_kb = val_kb
                elif key == "MemAvailable":
                    available_kb = val_kb
                elif key == "MemFree":
                    free_kb = val_kb
                elif key == "Buffers":
                    buffers_kb = val_kb
                elif key == "Cached":
                    cached_kb = val_kb
    except Exception:
        pass

    if total_kb is None or total_kb <= 0:
        return {
            "total_bytes": 0,
            "available_bytes": 0,
            "used_bytes": 0,
            "percent": 0.0,
            "status": "unknown",
            "error": "meminfo_unavailable",
        }

    total_bytes = total_kb * 1024

    if available_kb is not None:
        available_bytes = available_kb * 1024
    else:
        # Fallback for older kernels/containers without MemAvailable
        f = (free_kb or 0) * 1024
        b = (buffers_kb or 0) * 1024
        c = (cached_kb or 0) * 1024
        available_bytes = min(total_bytes, f + b + c)

    used_bytes = max(0, total_bytes - available_bytes)
    percent = round((used_bytes / total_bytes) * 100.0, 1)

    if percent >= critical_percent:
        status = "critical"
    elif percent >= warn_percent:
        status = "warn"
    else:
        status = "ok"

    return {
        "total_bytes": total_bytes,
        "available_bytes": available_bytes,
        "used_bytes": used_bytes,
        "percent": percent,
        "status": status,
    }


def verify_service_source(
    status_path: str,
    workspace: str,
    *,
    expected_uid: Optional[int] = None,
    allow_symlinks: bool = False,
) -> Tuple[bool, Optional[str], Optional[os.stat_result]]:
    """Verify that status_path is a trusted, non-symlink file owned by current user."""
    if not isinstance(status_path, str) or not isinstance(workspace, str):
        return False, "invalid_path_type", None

    clean_path = os.path.abspath(status_path)
    clean_workspace = os.path.abspath(workspace)

    if not clean_path.startswith(clean_workspace + os.sep) and clean_path != clean_workspace:
        return False, "source_not_in_workspace", None

    if not allow_symlinks:
        if os.path.islink(status_path):
            return False, "source_symlink_rejected", None
        try:
            real_path = os.path.realpath(status_path)
            if real_path != clean_path:
                return False, "source_symlink_rejected", None
        except Exception:
            return False, "source_symlink_rejected", None

    if not os.path.exists(clean_path):
        return False, "missing_status_file", None

    if not os.path.isfile(clean_path):
        return False, "source_not_regular_file", None

    try:
        st = os.stat(clean_path)
    except Exception:
        return False, "unreadable_status_file", None

    target_uid = os.getuid() if expected_uid is None else expected_uid
    if st.st_uid != target_uid:
        return False, "source_ownership_mismatch", None

    if st.st_mode & 0o002:
        return False, "source_permissions_unsafe", None

    return True, None, st


def check_worker_status(
    workspace: str,
    session_tag: str,
    *,
    aplexer_bin: str = "aplexer",
    runner_fn: Optional[Callable[[List[str]], Any]] = None,
    max_output_bytes: int = MAX_APLEXER_OUTPUT_BYTES,
    timeout_s: float = 5.0,
) -> Tuple[str, Optional[str]]:
    """Check aplexer session worker state ('alive', 'broken', 'unknown')."""
    cmd = [aplexer_bin, "status", "--workspace", workspace, "--tag", session_tag, "--json"]
    try:
        if runner_fn is not None:
            proc = runner_fn(cmd)
            stdout = getattr(proc, "stdout", "")
            stderr = getattr(proc, "stderr", "")
            raw_stdout = stdout if isinstance(stdout, bytes) else (stdout or "").encode("utf-8")
            raw_stderr = stderr if isinstance(stderr, bytes) else (stderr or "").encode("utf-8")
            if len(raw_stdout) > max_output_bytes or len(raw_stderr) > max_output_bytes:
                return "unknown", "aplexer_output_excess"
            if getattr(proc, "returncode", 0) != 0:
                return "unknown", "aplexer_command_failed"
            raw_bytes = raw_stdout
        else:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            raw_stdout_chunks: List[bytes] = []
            raw_stderr_chunks: List[bytes] = []
            stdout_len = 0
            stderr_len = 0

            sel = selectors.DefaultSelector()
            if proc.stdout:
                os.set_blocking(proc.stdout.fileno(), False)
                sel.register(proc.stdout, selectors.EVENT_READ, data="stdout")
            if proc.stderr:
                os.set_blocking(proc.stderr.fileno(), False)
                sel.register(proc.stderr, selectors.EVENT_READ, data="stderr")

            deadline = time.monotonic() + timeout_s
            timed_out = False
            excess = False

            try:
                while sel.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break

                    events = sel.select(timeout=max(0.01, min(remaining, 0.5)))
                    for key, _ in events:
                        fd = key.fileobj
                        stream_type = key.data
                        try:
                            chunk = os.read(fd.fileno(), 4096) if hasattr(fd, "fileno") else b""
                        except (BlockingIOError, OSError):
                            chunk = b""
                        if not chunk:
                            try:
                                sel.unregister(fd)
                            except Exception:
                                pass
                            continue

                        if stream_type == "stdout":
                            stdout_len += len(chunk)
                            raw_stdout_chunks.append(chunk)
                            if stdout_len > max_output_bytes:
                                excess = True
                                break
                        else:
                            stderr_len += len(chunk)
                            raw_stderr_chunks.append(chunk)
                            if stderr_len > max_output_bytes:
                                excess = True
                                break

                    if excess:
                        break

                    if proc.poll() is not None:
                        # Process exited, drain remaining if available
                        for key in list(sel.get_map().values()):
                            fd = key.fileobj
                            stream_type = key.data
                            try:
                                while True:
                                    chunk = os.read(fd.fileno(), 4096)
                                    if not chunk:
                                        break
                                    if stream_type == "stdout":
                                        stdout_len += len(chunk)
                                        raw_stdout_chunks.append(chunk)
                                        if stdout_len > max_output_bytes:
                                            excess = True
                                            break
                                    else:
                                        stderr_len += len(chunk)
                                        raw_stderr_chunks.append(chunk)
                                        if stderr_len > max_output_bytes:
                                            excess = True
                                            break
                            except (BlockingIOError, OSError):
                                pass
                            try:
                                sel.unregister(fd)
                            except Exception:
                                pass
                        break

            finally:
                try:
                    sel.close()
                except Exception:
                    pass
                if proc.stdout:
                    try:
                        proc.stdout.close()
                    except Exception:
                        pass
                if proc.stderr:
                    try:
                        proc.stderr.close()
                    except Exception:
                        pass

            if excess:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
                return "unknown", "aplexer_output_excess"

            if timed_out:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
                return "unknown", "aplexer_timeout"

            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
                return "unknown", "aplexer_timeout"

            if proc.returncode != 0:
                return "unknown", "aplexer_command_failed"

            raw_bytes = b"".join(raw_stdout_chunks)

        data = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(data, dict):
            return "unknown", "aplexer_output_not_dict"

        worker_alive = data.get("worker_alive")
        state = data.get("state")

        if state == "broken" or worker_alive is False:
            return "broken", "worker_broken"
        elif worker_alive is True:
            return "alive", None
        else:
            return "unknown", "worker_unknown"
    except subprocess.TimeoutExpired:
        return "unknown", "aplexer_timeout"
    except Exception as e:
        return "unknown", f"aplexer_error:{type(e).__name__}"


def read_status_file(
    status_path: str,
    *,
    now: float,
    workspace: Optional[str] = None,
    open_fn: Optional[Callable[..., Any]] = None,
    max_bytes: int = MAX_STATUS_FILE_BYTES,
    expected_uid: Optional[int] = None,
    allow_symlinks: bool = False,
) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    """Read status.json via anchored descriptor chain and extract (age_s, observation_iso, error_reason)."""
    mtime: float
    content: str

    if open_fn is not None and open_fn is not open:
        # Mock/test open hook
        try:
            with open_fn(status_path, "r", encoding="utf-8") as f:
                content = f.read(max_bytes + 1)
        except PermissionError:
            return None, None, "unreadable_status_file"
        except Exception:
            return None, None, "unreadable_status_file"

        if len(content) > max_bytes:
            return None, None, "status_file_oversized"

        try:
            st = os.stat(status_path)
            mtime = st.st_mtime
        except Exception:
            mtime = now
    else:
        # Anchored descriptor chain:
        clean_status = os.path.abspath(status_path)
        clean_ws = os.path.abspath(workspace) if workspace else os.path.dirname(clean_status)

        if workspace:
            if not clean_status.startswith(clean_ws + os.sep) and clean_status != clean_ws:
                return None, None, "source_not_in_workspace"
            rel = os.path.relpath(clean_status, clean_ws)
        else:
            rel = os.path.basename(clean_status)
            clean_ws = os.path.dirname(clean_status)

        opened_fds: List[int] = []
        try:
            # 1. Walk from trusted rootfd through workspace path components with dir_fd and O_NOFOLLOW
            root_path = os.path.abspath(os.sep)
            root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            try:
                curr_fd = os.open(root_path, root_flags)
                opened_fds.append(curr_fd)
            except OSError as e:
                err = getattr(e, "errno", None)
                if err in (getattr(errno, "EACCES", 13), getattr(errno, "EPERM", 1)):
                    return None, None, "unreadable_status_file"
                return None, None, "unreadable_status_file"

            ws_parts = [p for p in clean_ws.split(os.sep) if p and p != "."]
            dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            if not allow_symlinks and hasattr(os, "O_NOFOLLOW"):
                dir_flags |= os.O_NOFOLLOW

            for part in ws_parts:
                try:
                    next_fd = os.open(part, dir_flags, dir_fd=curr_fd)
                    opened_fds.append(next_fd)
                    curr_fd = next_fd
                except OSError as e:
                    err = getattr(e, "errno", None)
                    if err in (getattr(errno, "ELOOP", 40), getattr(errno, "ENOTDIR", 20)):
                        return None, None, "source_symlink_rejected"
                    elif err == getattr(errno, "ENOENT", 2):
                        return None, None, "missing_status_file"
                    elif err in (getattr(errno, "EACCES", 13), getattr(errno, "EPERM", 1)):
                        return None, None, "unreadable_status_file"
                    return None, None, "unreadable_status_file"

            parts = [p for p in rel.split(os.sep) if p and p != "."]
            if not parts:
                return None, None, "source_not_regular_file"

            # 2. Step through intermediate directories below workspace with dir_fd and O_NOFOLLOW
            dir_parts = parts[:-1]
            leaf_name = parts[-1]

            for part in dir_parts:
                try:
                    next_fd = os.open(part, dir_flags, dir_fd=curr_fd)
                    opened_fds.append(next_fd)
                    curr_fd = next_fd
                except OSError as e:
                    err = getattr(e, "errno", None)
                    if err in (getattr(errno, "ELOOP", 40), getattr(errno, "ENOTDIR", 20)):
                        return None, None, "source_symlink_rejected"
                    elif err == getattr(errno, "ENOENT", 2):
                        return None, None, "missing_status_file"
                    elif err in (getattr(errno, "EACCES", 13), getattr(errno, "EPERM", 1)):
                        return None, None, "unreadable_status_file"
                    return None, None, "unreadable_status_file"

            # 3. Open final leaf file with dir_fd, O_NONBLOCK, and O_NOFOLLOW
            leaf_flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
            if not allow_symlinks and hasattr(os, "O_NOFOLLOW"):
                leaf_flags |= os.O_NOFOLLOW
            try:
                file_fd = os.open(leaf_name, leaf_flags, dir_fd=curr_fd)
                opened_fds.append(file_fd)
            except OSError as e:
                err = getattr(e, "errno", None)
                if err in (getattr(errno, "ELOOP", 40), getattr(errno, "ENOTDIR", 20)):
                    return None, None, "source_symlink_rejected"
                elif err == getattr(errno, "ENOENT", 2):
                    return None, None, "missing_status_file"
                elif err in (getattr(errno, "EACCES", 13), getattr(errno, "EPERM", 1)):
                    return None, None, "unreadable_status_file"
                return None, None, "unreadable_status_file"

            # 4. fstat on the opened file descriptor
            try:
                st = os.fstat(file_fd)
                if not stat.S_ISREG(st.st_mode):
                    return None, None, "source_not_regular_file"

                target_uid = os.getuid() if expected_uid is None else expected_uid
                if st.st_uid != target_uid:
                    return None, None, "source_ownership_mismatch"

                if st.st_mode & 0o002:
                    return None, None, "source_permissions_unsafe"

                mtime = st.st_mtime
                raw_bytes = os.read(file_fd, max_bytes + 1)
            except Exception:
                return None, None, "unreadable_status_file"

        finally:
            for fd in reversed(opened_fds):
                try:
                    os.close(fd)
                except Exception:
                    pass

        if len(raw_bytes) > max_bytes:
            return None, None, "status_file_oversized"

        try:
            content = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return None, None, "corrupt_status_file"

    # Future mtime check (clock skew)
    if mtime > now + 60.0:
        dt_future = datetime.fromtimestamp(mtime, tz=timezone.utc)
        obs_future = dt_future.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return None, obs_future, "future_observation_timestamp"

    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            return None, None, "corrupt_status_file"
    except Exception:
        return None, None, "corrupt_status_file"

    mtime_age_s = max(0.0, now - mtime)
    raw_ts = data.get("timestamp")
    json_epoch: Optional[float] = None
    observation_iso: Optional[str] = None

    if isinstance(raw_ts, str) and raw_ts:
        try:
            dt = datetime.fromisoformat(raw_ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            json_epoch = dt.timestamp()
            observation_iso = dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        except Exception:
            json_epoch = None

    if json_epoch is not None:
        if json_epoch > now + 60.0:
            return None, observation_iso, "future_observation_timestamp"
        json_age_s = max(0.0, now - json_epoch)
        # Legacy contract: fresh JSON timestamp MUST NOT mask a stale file mtime!
        effective_age_s = max(mtime_age_s, json_age_s)
    else:
        dt_mtime = datetime.fromtimestamp(mtime, tz=timezone.utc)
        observation_iso = dt_mtime.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        effective_age_s = mtime_age_s

    if effective_age_s > SUPERVISOR_STALE_THRESHOLD_S:
        return round(effective_age_s, 1), observation_iso, "stale_status_file"

    return round(effective_age_s, 1), observation_iso, None


def collect_service_status(
    service_def: Dict[str, Any],
    *,
    now: float,
    expected_uid: Optional[int] = None,
    allow_symlinks: bool = False,
    aplexer_bin: str = "aplexer",
    runner_fn: Optional[Callable[[List[str]], Any]] = None,
    open_fn: Callable[..., Any] = open,
) -> Dict[str, Any]:
    """Collect bounded status for one supervisor service."""
    service_id = str(service_def.get("service_id", "supervision"))
    workspace = str(service_def.get("workspace", ""))
    session_tag = str(service_def.get("session_tag", "experiment-supervision"))
    status_path = str(service_def.get("status_path", ""))

    worker, worker_reason = check_worker_status(
        workspace, session_tag, aplexer_bin=aplexer_bin, runner_fn=runner_fn
    )

    valid, source_reason, _ = verify_service_source(
        status_path, workspace, expected_uid=expected_uid, allow_symlinks=allow_symlinks
    )

    if not valid:
        reason = source_reason or "invalid_source"
        if worker == "broken" and reason not in ("source_symlink_rejected", "source_ownership_mismatch"):
            reason = "worker_broken"
        return {
            "service_id": service_id,
            "worker": worker,
            "worker_state": worker,
            "status_file_age_s": None,
            "observed_at": None,
            "observation_time": None,
            "reason": reason,
        }

    age_s, observation_iso, file_reason = read_status_file(
        status_path,
        now=now,
        workspace=workspace,
        open_fn=open_fn,
        expected_uid=expected_uid,
        allow_symlinks=allow_symlinks,
    )

    if file_reason:
        reason = file_reason
    elif worker == "broken":
        reason = "worker_broken"
    elif worker == "unknown":
        reason = "worker_unknown"
    else:
        reason = "ok"

    return {
        "service_id": service_id,
        "worker": worker,
        "worker_state": worker,
        "status_file_age_s": age_s,
        "observed_at": observation_iso,
        "observation_time": observation_iso,
        "reason": reason,
    }


def collect_supervisor_services(
    machine_id: str,
    *,
    now: float,
    service_defs: Optional[List[Dict[str, Any]]] = None,
    expected_uid: Optional[int] = None,
    allow_symlinks: bool = False,
    aplexer_bin: str = "aplexer",
    runner_fn: Optional[Callable[[List[str]], Any]] = None,
    open_fn: Callable[..., Any] = open,
) -> List[Dict[str, Any]]:
    """Collect supervisor services for Hetzner or allowlisted machines."""
    if service_defs is not None:
        defs = service_defs
    elif machine_id in DEFAULT_HETZNER_MACHINE_IDS:
        defs = list(DEFAULT_HETZNER_SERVICES)
    else:
        return []

    results: List[Dict[str, Any]] = []
    for s_def in defs:
        if not isinstance(s_def, dict):
            continue
        svc_status = collect_service_status(
            s_def,
            now=now,
            expected_uid=expected_uid,
            allow_symlinks=allow_symlinks,
            aplexer_bin=aplexer_bin,
            runner_fn=runner_fn,
            open_fn=open_fn,
        )
        results.append(svc_status)
    return results


def collect_cloud_budget(
    evidence: Optional[Dict[str, Any]] = None,
    *,
    budget_cap_usd: Decimal = DEFAULT_BUDGET_CAP_USD,
    provenance: Optional[str] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Read-only aggregate cloud budget observation.

    Missing authentic billing credentials remain unknown with explicit
    coverage and provenance. Never invents spend or assumes missing accounts are free.
    """
    now_ts = now if now is not None else time.time()

    if evidence is None or not isinstance(evidence, dict):
        return {
            "budget_cap_usd": str(budget_cap_usd),
            "known_month_to_date_usd": None,
            "coverage_complete": False,
            "unknown_services": ["cloudflare", "hetzner"],
            "budget_status": "unknown",
            "reason": "missing_billing_credentials",
            "provenance": provenance or "unconfigured_missing_billing_credentials",
            "observed_at": None,
            "billing_period": None,
            "source_age_s": None,
        }

    raw_cap = evidence.get("budget_cap_usd")
    if raw_cap is not None:
        if isinstance(raw_cap, (bool, list, dict)):
            return {
                "budget_cap_usd": "5.00",
                "known_month_to_date_usd": None,
                "coverage_complete": False,
                "unknown_services": [],
                "budget_status": "unknown",
                "reason": "invalid_budget_cap_type",
                "provenance": provenance or evidence.get("provenance", "evidence_payload"),
                "observed_at": None,
                "billing_period": None,
                "source_age_s": None,
            }
        val_cap_str = str(raw_cap).strip()
        if not val_cap_str:
            return {
                "budget_cap_usd": "5.00",
                "known_month_to_date_usd": None,
                "coverage_complete": False,
                "unknown_services": [],
                "budget_status": "unknown",
                "reason": "empty_budget_cap_value",
                "provenance": provenance or evidence.get("provenance", "evidence_payload"),
                "observed_at": None,
                "billing_period": None,
                "source_age_s": None,
            }
        clean_cap_digits = val_cap_str[1:] if val_cap_str.startswith("-") else val_cap_str
        if not DECIMAL_STRING_PATTERN.match(clean_cap_digits):
            return {
                "budget_cap_usd": "5.00",
                "known_month_to_date_usd": None,
                "coverage_complete": False,
                "unknown_services": [],
                "budget_status": "unknown",
                "reason": "invalid_budget_cap_value",
                "provenance": provenance or evidence.get("provenance", "evidence_payload"),
                "observed_at": None,
                "billing_period": None,
                "source_age_s": None,
            }
        try:
            budget_cap_usd = Decimal(val_cap_str)
        except (InvalidOperation, TypeError, ValueError):
            return {
                "budget_cap_usd": "5.00",
                "known_month_to_date_usd": None,
                "coverage_complete": False,
                "unknown_services": [],
                "budget_status": "unknown",
                "reason": "invalid_budget_cap_value",
                "provenance": provenance or evidence.get("provenance", "evidence_payload"),
                "observed_at": None,
                "billing_period": None,
                "source_age_s": None,
            }
        if not budget_cap_usd.is_finite() or budget_cap_usd <= 0:
            return {
                "budget_cap_usd": "5.00",
                "known_month_to_date_usd": None,
                "coverage_complete": False,
                "unknown_services": [],
                "budget_status": "unknown",
                "reason": "non_finite_or_non_positive_cap",
                "provenance": provenance or evidence.get("provenance", "evidence_payload"),
                "observed_at": None,
                "billing_period": None,
                "source_age_s": None,
            }

    raw_observed_at = evidence.get("observed_at")
    observed_at_iso: Optional[str] = None
    source_age_s: Optional[float] = None
    if raw_observed_at is not None:
        try:
            if isinstance(raw_observed_at, (int, float)):
                obs_epoch = float(raw_observed_at)
                dt = datetime.fromtimestamp(obs_epoch, tz=timezone.utc)
                observed_at_iso = dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            elif isinstance(raw_observed_at, str):
                s = raw_observed_at.strip()
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                dt = datetime.fromisoformat(s)
                obs_epoch = dt.timestamp()
                observed_at_iso = dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            else:
                obs_epoch = None
        except Exception:
            obs_epoch = None

        if obs_epoch is None:
            return {
                "budget_cap_usd": str(budget_cap_usd),
                "known_month_to_date_usd": None,
                "coverage_complete": False,
                "unknown_services": [],
                "budget_status": "unknown",
                "reason": "invalid_source_observation_time",
                "provenance": provenance or evidence.get("provenance", "evidence_payload"),
                "observed_at": None,
                "billing_period": None,
                "source_age_s": None,
            }
        if obs_epoch > now_ts + 60.0:
            return {
                "budget_cap_usd": str(budget_cap_usd),
                "known_month_to_date_usd": None,
                "coverage_complete": False,
                "unknown_services": [],
                "budget_status": "unknown",
                "reason": "future_source_observation_time",
                "provenance": provenance or evidence.get("provenance", "evidence_payload"),
                "observed_at": observed_at_iso,
                "billing_period": None,
                "source_age_s": None,
            }
        source_age_s = max(0.0, round(now_ts - obs_epoch, 1))

    billing_period = evidence.get("billing_period")
    if billing_period is not None:
        billing_period = str(billing_period).strip()
    elif observed_at_iso is not None:
        billing_period = observed_at_iso[:7]

    raw_services = evidence.get("services")
    if not isinstance(raw_services, list):
        return {
            "budget_cap_usd": str(budget_cap_usd),
            "known_month_to_date_usd": None,
            "coverage_complete": False,
            "unknown_services": [],
            "budget_status": "unknown",
            "reason": "invalid_services_payload",
            "provenance": provenance or evidence.get("provenance", "evidence_payload"),
            "observed_at": observed_at_iso,
            "billing_period": billing_period,
            "source_age_s": source_age_s,
        }

    known = Decimal("0")
    has_known = False
    unknown_services: List[str] = []
    seen_services = set()
    sanitized_services: List[Dict[str, Any]] = []

    for item in raw_services:
        if not isinstance(item, dict):
            return {
                "budget_cap_usd": str(budget_cap_usd),
                "known_month_to_date_usd": None,
                "coverage_complete": False,
                "unknown_services": [],
                "budget_status": "unknown",
                "reason": "invalid_service_entry",
                "provenance": provenance or evidence.get("provenance", "evidence_payload"),
                "observed_at": observed_at_iso,
                "billing_period": billing_period,
                "source_age_s": source_age_s,
            }
        svc_name = str(item.get("service", "")).strip()
        if not svc_name or svc_name in seen_services:
            return {
                "budget_cap_usd": str(budget_cap_usd),
                "known_month_to_date_usd": None,
                "coverage_complete": False,
                "unknown_services": [],
                "budget_status": "unknown",
                "reason": "duplicate_or_empty_service_name",
                "provenance": provenance or evidence.get("provenance", "evidence_payload"),
                "observed_at": observed_at_iso,
                "billing_period": billing_period,
                "source_age_s": source_age_s,
            }
        seen_services.add(svc_name)

        val = item.get("month_to_date_usd")
        if val is None:
            unknown_services.append(svc_name)
            sanitized_services.append({"service": svc_name, "month_to_date_usd": None})
            continue

        if isinstance(val, (bool, list, dict)):
            return {
                "budget_cap_usd": str(budget_cap_usd),
                "known_month_to_date_usd": None,
                "coverage_complete": False,
                "unknown_services": [],
                "budget_status": "unknown",
                "reason": "invalid_amount_type",
                "provenance": provenance or evidence.get("provenance", "evidence_payload"),
                "observed_at": observed_at_iso,
                "billing_period": billing_period,
                "source_age_s": source_age_s,
            }

        val_str = str(val).strip()
        if not val_str:
            return {
                "budget_cap_usd": str(budget_cap_usd),
                "known_month_to_date_usd": None,
                "coverage_complete": False,
                "unknown_services": [],
                "budget_status": "unknown",
                "reason": "empty_amount_value",
                "provenance": provenance or evidence.get("provenance", "evidence_payload"),
                "observed_at": observed_at_iso,
                "billing_period": billing_period,
                "source_age_s": source_age_s,
            }
        clean_val_digits = val_str[1:] if val_str.startswith("-") else val_str
        if not DECIMAL_STRING_PATTERN.match(clean_val_digits):
            return {
                "budget_cap_usd": str(budget_cap_usd),
                "known_month_to_date_usd": None,
                "coverage_complete": False,
                "unknown_services": [],
                "budget_status": "unknown",
                "reason": "invalid_amount_value",
                "provenance": provenance or evidence.get("provenance", "evidence_payload"),
                "observed_at": observed_at_iso,
                "billing_period": billing_period,
                "source_age_s": source_age_s,
            }

        try:
            amount = Decimal(val_str)
        except (InvalidOperation, TypeError, ValueError):
            return {
                "budget_cap_usd": str(budget_cap_usd),
                "known_month_to_date_usd": None,
                "coverage_complete": False,
                "unknown_services": [],
                "budget_status": "unknown",
                "reason": "invalid_amount_value",
                "provenance": provenance or evidence.get("provenance", "evidence_payload"),
                "observed_at": observed_at_iso,
                "billing_period": billing_period,
                "source_age_s": source_age_s,
            }

        if not amount.is_finite() or amount < 0:
            return {
                "budget_cap_usd": str(budget_cap_usd),
                "known_month_to_date_usd": None,
                "coverage_complete": False,
                "unknown_services": [],
                "budget_status": "unknown",
                "reason": "non_finite_or_negative_amount",
                "provenance": provenance or evidence.get("provenance", "evidence_payload"),
                "observed_at": observed_at_iso,
                "billing_period": billing_period,
                "source_age_s": source_age_s,
            }

        known += amount
        has_known = True
        sanitized_services.append({"service": svc_name, "month_to_date_usd": str(amount)})

    # Coverage verification: a boolean complete_account_coverage=True config flag
    # MUST NOT establish current real billing by itself.
    expected_services = evidence.get("expected_services")
    expected_missing = []
    if isinstance(expected_services, list):
        for exp in expected_services:
            exp_str = str(exp).strip()
            if exp_str and exp_str not in seen_services:
                expected_missing.append(exp_str)

    current_month = datetime.fromtimestamp(now_ts, timezone.utc).strftime("%Y-%m")
    is_period_mismatch = billing_period is not None and billing_period != current_month
    is_prior_period = billing_period is not None and billing_period < current_month
    is_stale_source = source_age_s is not None and source_age_s > 30 * 86400.0

    complete = (
        evidence.get("complete_account_coverage") is True
        and bool(raw_services)
        and not unknown_services
        and not expected_missing
        and has_known
        and observed_at_iso is not None
        and not is_stale_source
        and not is_period_mismatch
    )

    if has_known and known > budget_cap_usd:
        budget_status = "over_budget"
        reason = "known_spend_exceeds_budget"
    elif complete:
        budget_status = "known_within_budget"
        reason = "complete_coverage_within_budget"
    else:
        budget_status = "unknown"
        if expected_missing:
            reason = "missing_expected_services"
        elif unknown_services:
            reason = "incomplete_billing_coverage"
        elif is_stale_source:
            reason = "stale_source_observation"
        elif is_prior_period:
            reason = "prior_billing_period"
        elif is_period_mismatch:
            reason = "billing_period_mismatch"
        elif observed_at_iso is None:
            reason = "missing_source_observation_time"
        else:
            reason = "missing_coverage_attestation"

    return {
        "budget_cap_usd": str(budget_cap_usd),
        "known_month_to_date_usd": str(known) if has_known else None,
        "coverage_complete": complete,
        "unknown_services": sorted(list(set(unknown_services + expected_missing))),
        "budget_status": budget_status,
        "reason": reason,
        "provenance": provenance or evidence.get("provenance", "evidence_payload"),
        "observed_at": observed_at_iso,
        "billing_period": billing_period,
        "source_age_s": source_age_s,
        "services": sanitized_services,
    }


def collect_storage_hygiene(
    paths: Optional[List[str]] = None,
    *,
    soft_bytes: int = DEFAULT_STORAGE_SOFT_BYTES,
    hard_bytes: int = DEFAULT_STORAGE_HARD_BYTES,
    max_entries: int = MAX_STORAGE_SCAN_ENTRIES,
    expected_uid: Optional[int] = None,
    stat_fn: Callable[[str], Any] = os.stat,
    walk_fn: Any = None,
) -> List[Dict[str, Any]]:
    """Bounded, non-destructive metadata observation of private directory storage.

    Never reads file contents, tokens, or private payload data.
    Never performs automated deletion or log rotation.
    Thresholds trigger warnings; they do not silently pause execution.
    Protects against symlink leaf and ancestor traversal using descriptor-relative
    pinned handles and O_NOFOLLOW.
    """
    target_paths = paths if paths is not None else [os.path.join(os.getcwd(), ".local")]
    results: List[Dict[str, Any]] = []
    target_uid = os.getuid() if expected_uid is None else expected_uid

    for raw_path in target_paths:
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        clean_path = os.path.abspath(raw_path)
        if not os.path.exists(clean_path):
            results.append({
                "path": clean_path,
                "used_bytes": 0,
                "soft_bytes": soft_bytes,
                "hard_bytes": hard_bytes,
                "state": "missing",
                "policy": "preserve all archives and unresolved evidence; no automatic deletion",
                "error": "path_not_found",
            })
            continue

        p_obj = Path(clean_path)

        # 1. Static ancestor hierarchy symlink check
        curr = p_obj
        has_symlink_ancestor = False
        while curr != curr.parent:
            if os.path.islink(str(curr)):
                has_symlink_ancestor = True
                break
            curr = curr.parent

        if has_symlink_ancestor:
            results.append({
                "path": clean_path,
                "used_bytes": 0,
                "soft_bytes": soft_bytes,
                "hard_bytes": hard_bytes,
                "state": "warn",
                "policy": "preserve all archives and unresolved evidence; no automatic deletion",
                "error": "symlink_storage_root_rejected",
            })
            continue

        # 2. Pinned directory FD chain open with O_NOFOLLOW (POSIX)
        target_fd: Optional[int] = None
        if hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"):
            try:
                curr_fd = os.open(p_obj.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    for part in p_obj.parts[1:]:
                        next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=curr_fd)
                        os.close(curr_fd)
                        curr_fd = next_fd
                    target_fd = curr_fd
                except OSError as exc:
                    os.close(curr_fd)
                    import errno
                    err_code = (
                        "symlink_storage_root_rejected"
                        if exc.errno in (errno.ELOOP, errno.ENOTDIR)
                        else f"open_dir_error:{exc.errno}"
                    )
                    results.append({
                        "path": clean_path,
                        "used_bytes": 0,
                        "soft_bytes": soft_bytes,
                        "hard_bytes": hard_bytes,
                        "state": "warn",
                        "policy": "preserve all archives and unresolved evidence; no automatic deletion",
                        "error": err_code,
                    })
                    continue
            except Exception as exc:
                results.append({
                    "path": clean_path,
                    "used_bytes": 0,
                    "soft_bytes": soft_bytes,
                    "hard_bytes": hard_bytes,
                    "state": "warn",
                    "policy": "preserve all archives and unresolved evidence; no automatic deletion",
                    "error": f"open_dir_error:{type(exc).__name__}",
                })
                continue

        # 3. Verify root ownership
        try:
            if target_fd is not None:
                st_root = os.fstat(target_fd)
            else:
                st_root = stat_fn(clean_path)
            if expected_uid is not None and st_root.st_uid != target_uid:
                if target_fd is not None:
                    os.close(target_fd)
                results.append({
                    "path": clean_path,
                    "used_bytes": 0,
                    "soft_bytes": soft_bytes,
                    "hard_bytes": hard_bytes,
                    "state": "warn",
                    "policy": "preserve all archives and unresolved evidence; no automatic deletion",
                    "error": "ownership_mismatch",
                })
                continue
        except Exception as exc:
            if target_fd is not None:
                os.close(target_fd)
            results.append({
                "path": clean_path,
                "used_bytes": 0,
                "soft_bytes": soft_bytes,
                "hard_bytes": hard_bytes,
                "state": "warn",
                "policy": "preserve all archives and unresolved evidence; no automatic deletion",
                "error": f"stat_error:{type(exc).__name__}",
            })
            continue

        # 4. Traversal with pinned directory descriptors
        total_size = 0
        entry_count = 0
        scan_truncated = False
        scan_error: Optional[str] = None

        try:
            if walk_fn is not None and walk_fn is not os.walk:
                # Custom walk function passed in tests
                for root, dirs, files in walk_fn(clean_path, followlinks=False):
                    pruned_dirs = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
                    for d in pruned_dirs:
                        entry_count += 1
                        if entry_count > max_entries:
                            scan_truncated = True
                            break
                    dirs[:] = pruned_dirs
                    if scan_truncated:
                        break
                    for fname in files:
                        entry_count += 1
                        if entry_count > max_entries:
                            scan_truncated = True
                            break
                        fpath = os.path.join(root, fname)
                        if os.path.islink(fpath):
                            continue
                        try:
                            st = stat_fn(fpath)
                            if expected_uid is not None and st.st_uid != target_uid:
                                continue
                            if stat.S_ISREG(st.st_mode):
                                total_size += st.st_size
                        except (OSError, PermissionError):
                            pass
                    if scan_truncated:
                        break
            elif target_fd is not None and hasattr(os, "fwalk"):
                # Pinned descriptor traversal using os.fwalk
                for root, dirs, files, dirfd in os.fwalk(dir_fd=target_fd, follow_symlinks=False):
                    pruned_dirs = []
                    for d in dirs:
                        try:
                            st_sub = os.stat(d, dir_fd=dirfd, follow_symlinks=False)
                            if stat.S_ISLNK(st_sub.st_mode):
                                continue
                            if expected_uid is not None and st_sub.st_uid != target_uid:
                                continue
                            entry_count += 1
                            if entry_count > max_entries:
                                scan_truncated = True
                                break
                            pruned_dirs.append(d)
                        except (OSError, PermissionError):
                            continue
                    dirs[:] = pruned_dirs
                    if scan_truncated:
                        break

                    for fname in files:
                        entry_count += 1
                        if entry_count > max_entries:
                            scan_truncated = True
                            break
                        try:
                            st = os.stat(fname, dir_fd=dirfd, follow_symlinks=False)
                            if stat.S_ISLNK(st.st_mode):
                                continue
                            if expected_uid is not None and st.st_uid != target_uid:
                                continue
                            if stat.S_ISREG(st.st_mode):
                                total_size += st.st_size
                        except (OSError, PermissionError):
                            pass
                    if scan_truncated:
                        break
            else:
                for root, dirs, files in os.walk(clean_path, followlinks=False):
                    pruned_dirs = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
                    for d in pruned_dirs:
                        entry_count += 1
                        if entry_count > max_entries:
                            scan_truncated = True
                            break
                    dirs[:] = pruned_dirs
                    if scan_truncated:
                        break
                    for fname in files:
                        entry_count += 1
                        if entry_count > max_entries:
                            scan_truncated = True
                            break
                        fpath = os.path.join(root, fname)
                        if os.path.islink(fpath):
                            continue
                        try:
                            st = stat_fn(fpath)
                            if expected_uid is not None and st.st_uid != target_uid:
                                continue
                            if stat.S_ISREG(st.st_mode):
                                total_size += st.st_size
                        except (OSError, PermissionError):
                            pass
                    if scan_truncated:
                        break
        except Exception as exc:
            scan_error = f"scan_error:{type(exc).__name__}"
        finally:
            if target_fd is not None:
                os.close(target_fd)

        if scan_truncated or scan_error:
            state = "warn"
        elif total_size >= hard_bytes:
            state = "warn"
        elif total_size >= soft_bytes:
            state = "warn"
        else:
            state = "ok"

        entry_result: Dict[str, Any] = {
            "path": clean_path,
            "used_bytes": total_size,
            "soft_bytes": soft_bytes,
            "hard_bytes": hard_bytes,
            "state": state,
            "policy": "preserve all archives and unresolved evidence; no automatic deletion",
        }
        if scan_truncated:
            entry_result["truncated"] = True
        if scan_error:
            entry_result["error"] = scan_error

        results.append(entry_result)

    return results


def collect_host_health(
    machine_id: str,
    *,
    getloadavg_fn: Optional[Callable[[], Tuple[float, float, float]]] = None,
    meminfo_path: str = "/proc/meminfo",
    warn_percent: float = DEFAULT_WARN_PERCENT,
    critical_percent: float = DEFAULT_CRITICAL_PERCENT,
    open_fn: Callable[..., Any] = open,
    time_fn: Callable[[], str] = utc_now_iso,
    services: Optional[List[Dict[str, Any]]] = None,
    budget: Optional[Dict[str, Any]] = None,
    storage: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Collect complete host health snapshot (load averages + memory + services + budget + storage)."""
    load = collect_load_averages(getloadavg_fn)
    memory = collect_memory_stats(
        meminfo_path=meminfo_path,
        warn_percent=warn_percent,
        critical_percent=critical_percent,
        open_fn=open_fn,
    )

    payload: Dict[str, Any] = {
        "machine_id": machine_id,
        "observed_at": time_fn(),
        "load": load,
        "memory": memory,
    }
    if services is not None:
        payload["services"] = services
    if budget is not None:
        payload["budget"] = budget
    if storage is not None:
        payload["storage"] = storage

    return payload


def _resolve_origin_and_token(
    base_url: Optional[str] = None,
    token: Optional[str] = None,
) -> Tuple[str, str]:
    cfg = config.load_config()
    clean_base = (
        base_url
        or os.environ.get("HUB_URL")
        or os.environ.get("HUB_BASE_URL")
        or cfg.get("base_url")
        or config.DEFAULT_BASE_URL
    )
    api.check_url(clean_base)
    env_token = os.environ.get("HUB_TOKEN") or os.environ.get("HUB_JOB_TOKEN")
    access_token = token or env_token or auth.get_access_token()
    if not access_token:
        raise ValueError("Missing host access token for health monitoring")
    return clean_base.rstrip("/"), access_token


def post_host_health_observation(
    payload: Dict[str, Any],
    *,
    base_url: Optional[str] = None,
    token: Optional[str] = None,
    endpoint: str = "/api/monitoring/health",
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """Post host health payload to Hub with host bearer token."""
    clean_base, access_token = _resolve_origin_and_token(base_url, token)
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_PAYLOAD_BYTES:
        raise ValueError("Host health observation payload exceeds 32 KiB limit")

    url = f"{clean_base}{endpoint}"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        method="POST",
    )
    return api.safe_http_json(req, timeout=timeout)


class HostHealthMonitor:
    """Linux stdlib host health monitor (load + memory) for worker nodes.

    Constructed in session_host with trusted paths.
    ``step(submit=True)`` runs a single rate-limited cycle from the observer thread.
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        machine_id: Optional[str] = None,
        min_interval_s: Optional[float] = None,
        submit_fn: Optional[Callable[..., Any]] = None,
        getloadavg_fn: Optional[Callable[[], Tuple[float, float, float]]] = None,
        meminfo_path: str = "/proc/meminfo",
        open_fn: Callable[..., Any] = open,
        time_fn: Optional[Callable[[], float]] = None,
        monotonic_fn: Optional[Callable[[], float]] = None,
        services: Optional[List[Dict[str, Any]]] = None,
        runner_fn: Optional[Callable[[List[str]], Any]] = None,
        aplexer_bin: str = "aplexer",
        allow_symlinks: bool = False,
        expected_uid: Optional[int] = None,
        budget_evidence: Optional[Dict[str, Any]] = None,
        storage_paths: Optional[List[str]] = None,
        storage_soft_bytes: Optional[int] = None,
        storage_hard_bytes: Optional[int] = None,
    ):
        self._config_path = config_path or resolve_config_path()
        self._config_override = config
        self._machine_id_override = machine_id
        self._min_interval_override = min_interval_s
        self._submit_fn = submit_fn or post_host_health_observation
        self._getloadavg_fn = getloadavg_fn
        self._meminfo_path = meminfo_path
        self._open_fn = open_fn
        self._time_fn = time_fn or time.time
        self._monotonic_fn = monotonic_fn or time.monotonic
        self._services_override = services
        self._runner_fn = runner_fn
        self._aplexer_bin = aplexer_bin
        self._allow_symlinks = allow_symlinks
        self._expected_uid = expected_uid
        self._budget_evidence = budget_evidence
        self._storage_paths = storage_paths
        self._storage_soft_bytes = storage_soft_bytes
        self._storage_hard_bytes = storage_hard_bytes

        self._last_run_monotonic = 0.0
        self._last_result: Optional[Dict[str, Any]] = None

    def _load_config(self) -> Optional[Dict[str, Any]]:
        if self._config_override is not None:
            cfg = dict(self._config_override)
        else:
            if not os.path.isfile(self._config_path):
                # When config file is absent, enable by default if machine_id is provided
                cfg = {"enabled": True}
            else:
                try:
                    with open(self._config_path, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                except Exception:
                    return None

        if not isinstance(cfg, dict):
            return None

        for k in list(cfg.keys()):
            if k not in _ALLOWED_CONFIG_KEYS:
                del cfg[k]

        return cfg

    def step(self, submit: bool = True) -> Dict[str, Any]:
        """Execute one isolated health collection round.

        Never raises: all errors are captured in the returned dict.
        """
        now_mono = self._monotonic_fn()
        cfg = self._load_config()

        if cfg is None:
            return {"disabled": True, "rate_limited": False, "posted": False, "errors": ["invalid_config"]}

        if not cfg.get("enabled", True):
            return {"disabled": True, "rate_limited": False, "posted": False, "errors": []}

        min_interval = self._min_interval_override or cfg.get("min_interval_s", DEFAULT_MIN_INTERVAL_S)
        try:
            min_interval = max(float(min_interval), MIN_INTERVAL_FLOOR_S)
        except (ValueError, TypeError):
            min_interval = float(MIN_INTERVAL_FLOOR_S)

        if self._last_run_monotonic > 0 and (now_mono - self._last_run_monotonic) < min_interval:
            return {"disabled": False, "rate_limited": True, "posted": False, "errors": []}

        self._last_run_monotonic = now_mono

        machine_id = self._machine_id_override or cfg.get("machine_id")
        if not machine_id:
            try:
                base_cfg = config.load_config()
                machine_id = base_cfg.get("machine_id")
            except Exception:
                pass

        if not machine_id:
            return {"disabled": False, "rate_limited": False, "posted": False, "errors": ["missing_machine_id"]}

        warn_percent = float(cfg.get("warn_threshold_percent", DEFAULT_WARN_PERCENT))
        critical_percent = float(cfg.get("critical_threshold_percent", DEFAULT_CRITICAL_PERCENT))
        endpoint = cfg.get("endpoint", "/api/monitoring/health")

        errors: List[str] = []
        now_epoch = self._time_fn()
        services_cfg = self._services_override or cfg.get("services")
        services_list = collect_supervisor_services(
            machine_id=machine_id,
            now=now_epoch,
            service_defs=services_cfg,
            expected_uid=self._expected_uid,
            allow_symlinks=self._allow_symlinks,
            aplexer_bin=self._aplexer_bin,
            runner_fn=self._runner_fn,
            open_fn=self._open_fn,
        )

        from .service_health import collect_runtime_services
        runtime_services = collect_runtime_services(
            cfg.get("runtime_services"), now=now_epoch,
            cooldown_s=cfg.get("runtime_cooldown_s", 300))
        # Service IDs are one namespace. Conflicting configuration cannot replace
        # an independently confirmed positive with unknown/healthy data.
        merged = {}
        for service in services_list + runtime_services:
            key = service["service_id"]
            if key not in merged or service.get("worker") == "broken":
                merged[key] = service
        services_list = sorted(merged.values(), key=lambda row: row.get("worker") != "broken")[:16]

        budget_cfg = self._budget_evidence if self._budget_evidence is not None else cfg.get("budget")
        budget_payload = collect_cloud_budget(budget_cfg, now=now_epoch)

        storage_paths = self._storage_paths if self._storage_paths is not None else cfg.get("storage_paths")
        storage_soft = self._storage_soft_bytes or cfg.get("storage_soft_bytes", DEFAULT_STORAGE_SOFT_BYTES)
        storage_hard = self._storage_hard_bytes or cfg.get("storage_hard_bytes", DEFAULT_STORAGE_HARD_BYTES)
        storage_payload = (
            collect_storage_hygiene(
                paths=storage_paths,
                soft_bytes=storage_soft,
                hard_bytes=storage_hard,
                expected_uid=self._expected_uid,
            )
            if (storage_paths is not None or "storage_paths" in cfg)
            else None
        )

        try:
            payload = collect_host_health(
                machine_id=machine_id,
                getloadavg_fn=self._getloadavg_fn,
                meminfo_path=self._meminfo_path,
                warn_percent=warn_percent,
                critical_percent=critical_percent,
                open_fn=self._open_fn,
                services=services_list if services_list else None,
                budget=budget_payload,
                storage=storage_payload,
            )
        except Exception as e:
            return {"disabled": False, "rate_limited": False, "posted": False, "errors": [f"collection_error:{type(e).__name__}"]}

        posted = False
        if submit:
            try:
                self._submit_fn(payload, endpoint=endpoint)
                posted = True
            except Exception as e:
                errors.append(f"submit_error:{type(e).__name__}")

        result = {
            "disabled": False,
            "rate_limited": False,
            "posted": posted,
            "payload": payload,
            "errors": errors,
        }
        self._last_result = result
        return result
