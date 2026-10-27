"""Linux stdlib disk space monitoring, threshold alerting and bounded consumer scanning.

Provides:
- Native Linux stdlib statvfs and stat filesystem metrics
- Deduplicated underlying filesystems (duplicate dev IDs are aliases, not duplicate pressure)
- Meaningful mounts: /, /home, /data, /tmp
- Bounded top consumer scanning triggered only when threshold is crossed (>=85% warn, >=95% critical)
- Strict bounds: depth, time limit, count limit, filenames only, never content read/deletion
- Rate-limited, configuration-driven collector hook for session_host observer threads
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import api, auth, config

CONFIG_FILENAME = "disk-monitor.json"
DEFAULT_MIN_INTERVAL_S = 60
MIN_INTERVAL_FLOOR_S = 60
MAX_INTERVAL_S = 3600
DEFAULT_MOUNTS = ["/", "/home", "/data", "/tmp"]
DEFAULT_ALLOWED_ROOTS = ["/data", "/home", "/tmp"]
DEFAULT_WARN_PERCENT = 85.0
DEFAULT_CRITICAL_PERCENT = 95.0
DEFAULT_TOP_CONSUMERS_LIMIT = 10
MAX_TOP_CONSUMERS_LIMIT = 50
DEFAULT_TOP_CONSUMERS_DEPTH = 2
MAX_TOP_CONSUMERS_DEPTH = 4
DEFAULT_SCAN_TIMEOUT_S = 2.0
MAX_SCAN_TIMEOUT_S = 10.0
DEFAULT_MAX_ENTRIES_SCANNED = 2000
MAX_PAYLOAD_BYTES = 32 * 1024

_ALLOWED_CONFIG_KEYS = frozenset({
    "enabled",
    "machine_id",
    "min_interval_s",
    "mounts",
    "warn_threshold_percent",
    "critical_threshold_percent",
    "allowed_roots",
    "top_consumers_limit",
    "top_consumers_depth",
    "scan_timeout_s",
    "endpoint",
})


def resolve_config_path(explicit: str | None = None) -> str:
    """Resolve trusted disk monitor config path. Explicit wins."""
    if explicit:
        return explicit
    env = os.environ.get("HUB_DISK_MONITOR_CONFIG")
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


def scan_top_consumers(
    root_path: str,
    target_dev: int,
    *,
    max_depth: int = DEFAULT_TOP_CONSUMERS_DEPTH,
    limit: int = DEFAULT_TOP_CONSUMERS_LIMIT,
    timeout_s: float = DEFAULT_SCAN_TIMEOUT_S,
    max_entries: int = DEFAULT_MAX_ENTRIES_SCANNED,
    stat_fn: Callable[[str], Any] = os.stat,
    listdir_fn: Callable[[str], List[str]] = os.listdir,
    monotonic_fn: Callable[[], float] = time.monotonic,
    islink_fn: Optional[Callable[[str], bool]] = None,
    isdir_fn: Optional[Callable[[str], bool]] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Bounded shallow scan for largest entries in root_path on target_dev.

    Strict bounds:
    - Never crosses filesystem boundaries (st_dev must match target_dev)
    - Stops when monotonic_fn() - start >= timeout_s or scanned_count >= max_entries
    - Never follows symlinks or symlink escapes
    - Aggregate directory sizes instead of raw 4096 inode size
    - Candidates kept strictly bounded in memory (never stores all scanned entries)
    - Filenames and sizes only; never opens file contents; never alters files
    """
    start_time = monotonic_fn()
    truncated = False
    scanned_entries = 0
    _islink = islink_fn or os.path.islink
    _isdir = isdir_fn or os.path.isdir

    candidates: List[Tuple[str, int]] = []

    def _add_candidate(path: str, size: int) -> None:
        if size <= 0:
            return
        candidates.append((path, size))
        # Keep candidate buffer bounded in memory (never holds millions of items)
        if len(candidates) > max(limit * 4, 100):
            candidates.sort(key=lambda x: x[1], reverse=True)
            del candidates[max(limit * 2, 50):]

    def _allocated_bytes(st: Any) -> int:
        blocks = getattr(st, "st_blocks", None)
        if blocks is not None and blocks > 0:
            return blocks * 512
        return getattr(st, "st_size", 0)

    def _walk(curr_path: str, curr_depth: int) -> int:
        nonlocal truncated, scanned_entries
        if monotonic_fn() - start_time >= timeout_s or scanned_entries >= max_entries:
            truncated = True
            return 0
        if curr_depth > max_depth:
            return 0

        try:
            items = listdir_fn(curr_path)
        except (OSError, PermissionError):
            return 0

        dir_aggregate = 0

        for item in items:
            if monotonic_fn() - start_time >= timeout_s or scanned_entries >= max_entries:
                truncated = True
                break

            scanned_entries += 1
            full_path = os.path.join(curr_path, item)

            # Prevent symlink escapes or loops: inspect if link
            is_link = False
            try:
                is_link = _islink(full_path)
            except (OSError, PermissionError):
                is_link = False

            if is_link:
                # Never follow symlinks to avoid escapes or loops
                continue

            try:
                st = stat_fn(full_path)
            except (OSError, PermissionError):
                continue

            # Don't cross into a different filesystem
            if getattr(st, "st_dev", target_dev) != target_dev:
                continue

            is_dir = False
            try:
                is_dir = _isdir(full_path)
            except (OSError, PermissionError):
                is_dir = False

            if is_dir:
                sub_bytes = 0
                if curr_depth < max_depth:
                    sub_bytes = _walk(full_path, curr_depth + 1)
                alloc = max(_allocated_bytes(st), sub_bytes)
                dir_aggregate += alloc
                _add_candidate(full_path, alloc)
            else:
                alloc = _allocated_bytes(st)
                dir_aggregate += alloc
                _add_candidate(full_path, alloc)

        return dir_aggregate

    _walk(root_path, 1)

    # Sort descending by size
    candidates.sort(key=lambda x: x[1], reverse=True)
    top = [{"path": p, "size_bytes": sz} for p, sz in candidates[:limit]]
    return top, truncated


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
        raise ValueError("Missing host access token for disk monitoring")
    return clean_base.rstrip("/"), access_token


def post_disk_observation(
    payload: Dict[str, Any],
    *,
    base_url: Optional[str] = None,
    token: Optional[str] = None,
    endpoint: str = "/api/monitoring/disk",
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """Post disk monitoring payload to Hub with host bearer token."""
    clean_base, access_token = _resolve_origin_and_token(base_url, token)
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_PAYLOAD_BYTES:
        raise ValueError("Disk observation payload exceeds 32 KiB limit")

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


def _statvfs_from_disk_usage(path):
    """Windows shim: os.statvfs is absent; synthesize the statvfs fields collect() uses.

    shutil.disk_usage is byte-accurate (f_frsize=1); f_bfree/f_bavail carry
    the free value (Windows has no separate reserved-block concept here).
    """
    usage = shutil.disk_usage(path)
    return SimpleNamespace(f_blocks=usage.total, f_frsize=1, f_bfree=usage.free, f_bavail=usage.free)


def _is_windows_drive_path(mount: str) -> bool:
    """Accept C:/ and C:\\ mounts, but only on Windows so POSIX stays strict."""
    return os.name == "nt" and len(mount) >= 2 and mount[0].isalpha() and mount[1] == ":"


class DiskMonitor:
    """Linux stdlib disk space monitor for host nodes.

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
        statvfs_fn: Optional[Callable[[str], Any]] = None,
        stat_fn: Optional[Callable[[str], Any]] = None,
        listdir_fn: Optional[Callable[[str], List[str]]] = None,
        islink_fn: Optional[Callable[[str], bool]] = None,
        isdir_fn: Optional[Callable[[str], bool]] = None,
        exists_fn: Optional[Callable[[str], bool]] = None,
        time_fn: Optional[Callable[[], float]] = None,
        monotonic_fn: Optional[Callable[[], float]] = None,
    ):
        self.config_path = resolve_config_path(config_path)
        self._config_override = dict(config) if isinstance(config, dict) else None
        self._machine_id_override = machine_id
        self._min_interval_override = min_interval_s
        self.submit_fn = submit_fn or post_disk_observation
        self.statvfs_fn = statvfs_fn or getattr(os, "statvfs", None) or _statvfs_from_disk_usage
        self.stat_fn = stat_fn or os.stat
        self.listdir_fn = listdir_fn or os.listdir
        self.islink_fn = islink_fn or os.path.islink
        self.isdir_fn = isdir_fn or os.path.isdir
        if exists_fn is not None:
            self.exists_fn = exists_fn
        elif stat_fn is not None:
            def _check_exists(p: str) -> bool:
                try:
                    self.stat_fn(p)
                    return True
                except OSError:
                    return False
            self.exists_fn = _check_exists
        else:
            self.exists_fn = os.path.exists
        self.time_fn = time_fn or time.time
        self.monotonic_fn = monotonic_fn or time.monotonic

        self.last_collect_monotonic: float = -1e9
        self.last_posted: bool = False
        self.last_errors: List[str] = []

    def load_active_config(self) -> Optional[Dict[str, Any]]:
        """Load and validate configuration. Missing config file means disabled."""
        if self._config_override is not None:
            raw = dict(self._config_override)
        else:
            if not os.path.isfile(self.config_path):
                return None
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception as exc:
                self.last_errors = [f"config_load_error:{type(exc).__name__}"]
                return None

        if not isinstance(raw, dict):
            return None

        if raw.get("enabled") is False:
            return None

        # Build clean config with defaults
        cfg = {
            "machine_id": self._machine_id_override or raw.get("machine_id"),
            "min_interval_s": int(
                self._min_interval_override
                or raw.get("min_interval_s")
                or DEFAULT_MIN_INTERVAL_S
            ),
            "mounts": list(raw.get("mounts") or DEFAULT_MOUNTS),
            "warn_threshold_percent": float(
                raw.get("warn_threshold_percent") or DEFAULT_WARN_PERCENT
            ),
            "critical_threshold_percent": float(
                raw.get("critical_threshold_percent") or DEFAULT_CRITICAL_PERCENT
            ),
            "allowed_roots": list(raw.get("allowed_roots") or DEFAULT_ALLOWED_ROOTS),
            "top_consumers_limit": min(
                MAX_TOP_CONSUMERS_LIMIT,
                int(raw.get("top_consumers_limit") or DEFAULT_TOP_CONSUMERS_LIMIT),
            ),
            "top_consumers_depth": min(
                MAX_TOP_CONSUMERS_DEPTH,
                int(raw.get("top_consumers_depth") or DEFAULT_TOP_CONSUMERS_DEPTH),
            ),
            "scan_timeout_s": min(
                MAX_SCAN_TIMEOUT_S,
                float(raw.get("scan_timeout_s") or DEFAULT_SCAN_TIMEOUT_S),
            ),
            "endpoint": str(raw.get("endpoint") or "/api/monitoring/disk"),
        }

        # Clamp interval
        cfg["min_interval_s"] = max(
            MIN_INTERVAL_FLOOR_S, min(MAX_INTERVAL_S, cfg["min_interval_s"])
        )
        return cfg

    def collect(self, cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
        """Collect filesystem metrics across configured mounts and identify duplicate devices."""
        errors: List[str] = []
        filesystems: List[Dict[str, Any]] = []
        seen_devices: Dict[int, str] = {}  # dev_id -> primary mount_point
        scanned_devices: set[int] = set()

        for mount in cfg["mounts"]:
            if not isinstance(mount, str) or not (mount.startswith("/") or _is_windows_drive_path(mount)):
                errors.append(f"invalid_mount_path:{mount}")
                continue

            try:
                st = self.stat_fn(mount)
                vfs = self.statvfs_fn(mount)
            except (OSError, PermissionError) as exc:
                errors.append(f"stat_failed:{mount}:{type(exc).__name__}")
                filesystems.append({
                    "mount_point": mount,
                    "device_id": "unknown",
                    "is_primary": True,
                    "total_bytes": 0,
                    "used_bytes": 0,
                    "free_bytes": 0,
                    "percent": 0.0,
                    "status": "error",
                    "error": str(type(exc).__name__),
                })
                continue

            dev_id = getattr(st, "st_dev", 0)
            total = getattr(vfs, "f_blocks", 0) * getattr(vfs, "f_frsize", 0)
            free = getattr(vfs, "f_bavail", 0) * getattr(vfs, "f_frsize", 0)
            used = max(0, total - (getattr(vfs, "f_bfree", 0) * getattr(vfs, "f_frsize", 0)))
            denom = used + free
            percent = min(100.0, max(0.0, round((used / denom) * 100.0, 1))) if denom > 0 else 0.0

            # Threshold check
            if percent >= cfg["critical_threshold_percent"]:
                status = "critical"
            elif percent >= cfg["warn_threshold_percent"]:
                status = "warn"
            else:
                status = "ok"

            # Check device duplication (bind mounts or sub-mounts on same filesystem)
            if dev_id in seen_devices:
                is_primary = False
                primary_mount = seen_devices[dev_id]
            else:
                is_primary = True
                primary_mount = None
                seen_devices[dev_id] = mount

            top_consumers: List[Dict[str, Any]] = []
            top_consumers_truncated = False
            # Only perform bounded top-consumers scan when threshold is crossed
            # Avoid scanning duplicate devices across alias mounts
            if status in ("warn", "critical"):
                if dev_id not in scanned_devices:
                    scanned_devices.add(dev_id)
                    # Find matching allowed roots that reside on this mount / device
                    matching_roots = [
                        r for r in cfg["allowed_roots"]
                        if (r == mount or r.startswith(mount.rstrip("/") + "/"))
                    ]
                    for root in matching_roots:
                        if not self.exists_fn(root):
                            continue
                        try:
                            root_st = self.stat_fn(root)
                            if getattr(root_st, "st_dev", -1) != dev_id:
                                continue
                        except OSError:
                            continue

                        consumers, is_trunc = scan_top_consumers(
                            root,
                            dev_id,
                            max_depth=cfg["top_consumers_depth"],
                            limit=cfg["top_consumers_limit"],
                            timeout_s=cfg["scan_timeout_s"],
                            stat_fn=self.stat_fn,
                            listdir_fn=self.listdir_fn,
                            islink_fn=self.islink_fn,
                            isdir_fn=self.isdir_fn,
                            monotonic_fn=self.monotonic_fn,
                        )
                        top_consumers.extend(consumers)
                        if is_trunc:
                            top_consumers_truncated = True

                    # Sort combined consumers and trim
                    if top_consumers:
                        top_consumers.sort(key=lambda x: x["size_bytes"], reverse=True)
                        top_consumers = top_consumers[: cfg["top_consumers_limit"]]

            fs_entry: Dict[str, Any] = {
                "mount_point": mount,
                "device_id": str(dev_id),
                "is_primary": is_primary,
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": free,
                "percent": percent,
                "status": status,
            }
            if primary_mount:
                fs_entry["primary_mount"] = primary_mount
            if top_consumers:
                fs_entry["top_consumers"] = top_consumers
                fs_entry["top_consumers_truncated"] = top_consumers_truncated

            filesystems.append(fs_entry)

        payload = {
            "machine_id": cfg["machine_id"],
            "observed_at": utc_now_iso(),
            "filesystems": filesystems,
            "errors": errors,
        }
        return payload, errors

    def step(self, submit: bool = True) -> Dict[str, Any]:
        """Execute one isolated, rate-limited collection cycle. Never raises."""
        now_mono = self.monotonic_fn()

        cfg = self.load_active_config()
        if cfg is None:
            return {
                "rate_limited": False,
                "disabled": True,
                "posted": False,
                "filesystems": [],
                "errors": self.last_errors,
                "payload": None,
            }

        # Enforce rate limit
        elapsed = now_mono - self.last_collect_monotonic
        if elapsed < cfg["min_interval_s"]:
            return {
                "rate_limited": True,
                "disabled": False,
                "posted": False,
                "filesystems": [],
                "errors": [],
                "payload": None,
            }

        self.last_collect_monotonic = now_mono

        try:
            payload, errors = self.collect(cfg)
        except Exception as exc:
            err = f"collect_exception:{type(exc).__name__}"
            self.last_errors = [err]
            return {
                "rate_limited": False,
                "disabled": False,
                "posted": False,
                "filesystems": [],
                "errors": [err],
                "payload": None,
            }

        posted = False
        if submit:
            try:
                self.submit_fn(payload, endpoint=cfg["endpoint"])
                posted = True
            except Exception as exc:
                errors.append(f"submit_error:{type(exc).__name__}")

        self.last_posted = posted
        self.last_errors = errors

        return {
            "rate_limited": False,
            "disabled": False,
            "posted": posted,
            "filesystems": payload.get("filesystems", []),
            "errors": errors,
            "payload": payload,
        }
