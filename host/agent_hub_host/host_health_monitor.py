"""Host resource health monitor for Hetzner and Linux worker nodes.

Provides:
- Native Linux stdlib load averages (os.getloadavg 1m, 5m, 15m)
- Native Linux /proc/meminfo memory statistics (MemTotal, MemAvailable, used bytes, usage percent)
- Memory pressure evaluation (>=85% warn, >=95% critical)
- Pure standard library implementation (no psutil or external dependencies)
- Rate-limited, configuration-driven collector hook for session_host observer threads
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
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

_ALLOWED_CONFIG_KEYS = frozenset({
    "enabled",
    "machine_id",
    "min_interval_s",
    "warn_threshold_percent",
    "critical_threshold_percent",
    "endpoint",
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


def collect_host_health(
    machine_id: str,
    *,
    getloadavg_fn: Optional[Callable[[], Tuple[float, float, float]]] = None,
    meminfo_path: str = "/proc/meminfo",
    warn_percent: float = DEFAULT_WARN_PERCENT,
    critical_percent: float = DEFAULT_CRITICAL_PERCENT,
    open_fn: Callable[..., Any] = open,
    time_fn: Callable[[], str] = utc_now_iso,
) -> Dict[str, Any]:
    """Collect complete host health snapshot (load averages + memory)."""
    load = collect_load_averages(getloadavg_fn)
    memory = collect_memory_stats(
        meminfo_path=meminfo_path,
        warn_percent=warn_percent,
        critical_percent=critical_percent,
        open_fn=open_fn,
    )

    return {
        "machine_id": machine_id,
        "observed_at": time_fn(),
        "load": load,
        "memory": memory,
    }


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
        try:
            payload = collect_host_health(
                machine_id=machine_id,
                getloadavg_fn=self._getloadavg_fn,
                meminfo_path=self._meminfo_path,
                warn_percent=warn_percent,
                critical_percent=critical_percent,
                open_fn=self._open_fn,
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
