"""Persistent bounded Dashboard -> Hub metrics producer.

Owns ONLY the producer runtime wiring. Projection/validation stays in
:mod:`agent_hub_host.metrics` (``project_snapshot``/``validate_snapshot_payload``/
``post_metrics_snapshot``), transport stays in ``api.safe_http_json``.
This module adds no timer, service, dispatcher, or controller: ``MetricsProducer.step``
is a synchronous, rate-limited, isolated-error round that the existing
session_host loop calls from its single-inflight observer thread.

ABI (stable for head):
    producer = MetricsProducer(
        config_path=None,  # explicit trusted path (preferred)
        config=None,       # dict override (tests); wins over path
        machine_id=None,   # override; default config machine_id
        min_interval_s=None,  # override; default config or 300; floor 60
        exporter_runner=None,  # default: bounded subprocess of trusted exporter
        submit_fn=None,        # default: metrics.post_metrics_snapshot
        time_fn=None,          # default: time.time (wall clock for freshness)
        monotonic_fn=None,     # default: time.monotonic (cadence)
    )
    result = producer.step(submit=True)

- ``config_path`` explicit is preferred. When omitted, resolution is:
  ``$HUB_METRICS_PRODUCER_CONFIG`` then ``<cwd>/.local/session/
  metrics-producer.json`` then ``<cwd>/journal/metrics-producer.json``.
  Missing file means DISABLED (no collection, no HTTP, no exporter).
- ``step`` never raises for collection failures: every sub-phase is
  isolated and recorded in ``errors``. ``rate_limited`` is True when the
  call was skipped by cadence. ``disabled`` is True when no config.
- Real dashboard freshness is never synthesized: stale/missing/malformed
  exporter output keeps ``posted=False`` with a minimal failure class.
  No config/token/stderr/argv/report data ever enters logs; callers log
  only event + class (see ``session_host.metrics_step_once``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

CONFIG_FILENAME = "metrics-producer.json"
DEFAULT_MIN_INTERVAL_S = 300
MIN_INTERVAL_FLOOR_S = 60
MAX_INTERVAL_S = 3600
MAX_CONFIG_BYTES = 16 * 1024
MAX_EXPORTER_STDOUT_BYTES = 64 * 1024
DEFAULT_EXPORTER_TIMEOUT_S = 30
MAX_EXPORTER_TIMEOUT_S = 120
MIN_EXPORTER_TIMEOUT_S = 5
DEFAULT_POST_TIMEOUT_S = 10
MAX_POST_TIMEOUT_S = 30
MIN_POST_TIMEOUT_S = 5
STALE_AFTER_S = 7200
FUTURE_SKEW_S = 300

_ALLOWED_CONFIG_KEYS = frozenset({
    "machine_id",
    "min_interval_s",
    "dashboard_src",
    "output_dir",
    "exporter_argv",
    "exporter_timeout_s",
    "post_timeout_s",
})


class MetricsProducerError(ValueError):
    """Minimal failure class. Message never carries config/report data."""


def resolve_config_path(explicit: str | None = None) -> str:
    """Resolve the trusted producer config path. Explicit wins."""
    if explicit:
        return explicit
    env = os.environ.get("HUB_METRICS_PRODUCER_CONFIG")
    if env:
        return env
    cwd = os.getcwd()
    for candidate in (os.path.join(cwd, ".local", "session", CONFIG_FILENAME),
                      os.path.join(cwd, "journal", CONFIG_FILENAME)):
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(cwd, ".local", "session", CONFIG_FILENAME)


def utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _is_abs_home(path: str) -> bool:
    return isinstance(path, str) and path.startswith("/home/") and path == path.strip()


def _real_under(path: str, root: str) -> bool:
    try:
        real = os.path.realpath(path)
        anchor = os.path.realpath(root)
    except OSError:
        return False
    return real == anchor or real.startswith(anchor + os.sep)


def _load_config_file(path: str) -> dict | None:
    """None when missing (disabled). Raises MetricsProducerError when malformed."""
    if not isinstance(path, str) or not path:
        raise MetricsProducerError("invalid_config")
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size > MAX_CONFIG_BYTES:
        raise MetricsProducerError("invalid_config")
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise MetricsProducerError("invalid_config") from None
    if len(raw) > MAX_CONFIG_BYTES:
        raise MetricsProducerError("invalid_config")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise MetricsProducerError("invalid_config") from None
    if not isinstance(data, dict):
        raise MetricsProducerError("invalid_config")
    return data


def _clamp_interval(value: Any) -> int:
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MIN_INTERVAL_S
    if iv < MIN_INTERVAL_FLOOR_S:
        return MIN_INTERVAL_FLOOR_S
    if iv > MAX_INTERVAL_S:
        return MAX_INTERVAL_S
    return iv


def _clamp_timeout(value: Any, default: int) -> int:
    try:
        tv = int(value)
    except (TypeError, ValueError):
        return default
    if tv < MIN_EXPORTER_TIMEOUT_S:
        return MIN_EXPORTER_TIMEOUT_S
    if tv > MAX_EXPORTER_TIMEOUT_S:
        return MAX_EXPORTER_TIMEOUT_S
    return tv


def validate_config(raw: Any) -> dict:
    """Strict operator allowlist. Returns normalized config or raises."""
    if not isinstance(raw, dict):
        raise MetricsProducerError("invalid_config")
    keys = set(raw.keys())
    if not keys.issubset(_ALLOWED_CONFIG_KEYS):
        raise MetricsProducerError("invalid_config")
    machine_id = raw.get("machine_id")
    if machine_id is not None and (not isinstance(machine_id, str) or not machine_id.strip()):
        raise MetricsProducerError("invalid_config")
    min_interval = _clamp_interval(raw.get("min_interval_s", DEFAULT_MIN_INTERVAL_S))
    dashboard_src = raw.get("dashboard_src")
    if not isinstance(dashboard_src, str) or not _is_abs_home(dashboard_src):
        raise MetricsProducerError("invalid_config")
    # Realpath must stay under /home and must not escape via symlink.
    try:
        src_real = os.path.realpath(dashboard_src)
    except OSError:
        raise MetricsProducerError("invalid_config") from None
    if not src_real.startswith("/home/"):
        raise MetricsProducerError("invalid_config")
    output_dir = raw.get("output_dir")
    if not isinstance(output_dir, str) or not _is_abs_home(output_dir):
        raise MetricsProducerError("invalid_config")
    try:
        out_real = os.path.realpath(output_dir)
    except OSError:
        raise MetricsProducerError("invalid_config") from None
    if not out_real.startswith("/home/"):
        raise MetricsProducerError("invalid_config")
    if out_real == src_real or out_real.startswith(src_real + os.sep):
        raise MetricsProducerError("invalid_config")
    if "/global/data" in out_real or "/global/data" in output_dir:
        raise MetricsProducerError("invalid_config")
    exporter_argv = raw.get("exporter_argv")
    if exporter_argv is not None:
        if (not isinstance(exporter_argv, list) or not 1 <= len(exporter_argv) <= 16
                or not all(isinstance(a, str) and a and len(a) <= 512 for a in exporter_argv)):
            raise MetricsProducerError("invalid_config")
    exporter_timeout = _clamp_timeout(raw.get("exporter_timeout_s", DEFAULT_EXPORTER_TIMEOUT_S),
                                      DEFAULT_EXPORTER_TIMEOUT_S)
    post_timeout = raw.get("post_timeout_s", DEFAULT_POST_TIMEOUT_S)
    try:
        pt = int(post_timeout)
    except (TypeError, ValueError):
        pt = DEFAULT_POST_TIMEOUT_S
    if pt < MIN_POST_TIMEOUT_S:
        pt = MIN_POST_TIMEOUT_S
    if pt > MAX_POST_TIMEOUT_S:
        pt = MAX_POST_TIMEOUT_S
    return {
        "machine_id": machine_id.strip() if isinstance(machine_id, str) else None,
        "min_interval_s": min_interval,
        "dashboard_src": dashboard_src,
        "output_dir": output_dir,
        "exporter_argv": list(exporter_argv) if isinstance(exporter_argv, list) else None,
        "exporter_timeout_s": exporter_timeout,
        "post_timeout_s": pt,
    }


def _default_exporter_argv(cfg: dict) -> list:
    if cfg.get("exporter_argv"):
        return list(cfg["exporter_argv"])
    repo_scripts = Path(__file__).resolve().parents[2] / "scripts" / "metrics-dashboard-export.py"
    out_path = os.path.join(cfg["output_dir"], "metrics-snapshot.json")
    return [sys.executable, str(repo_scripts),
            "--dashboard-src", cfg["dashboard_src"], "--out", out_path]


def _run_exporter_subprocess(argv: list, timeout_s: int) -> tuple[dict | None, str]:
    """Bounded subprocess. Returns (snapshot|None, failure_class)."""
    if not argv or not isinstance(argv, list):
        return None, "exporter_failed"
    try:
        proc = subprocess.run(
            argv, shell=False, cwd="/home/alexey",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return None, "exporter_timeout"
    except (OSError, ValueError):
        return None, "exporter_failed"
    except Exception:
        return None, "exporter_failed"
    try:
        stdout = proc.stdout or b""
        if len(stdout) > MAX_EXPORTER_STDOUT_BYTES:
            return None, "exporter_failed"
        # Failure class only; raw stderr/argv never leave this module.
        if proc.returncode != 0:
            return None, "exporter_failed"
        # Exporter writes the snapshot file; stdout is only a bounded ack.
        # The file load happens in the caller via metrics.load_json_file.
        return {"ok": True}, ""
    except Exception:
        return None, "exporter_failed"


class MetricsProducer:
    """One rate-limited Dashboard->Hub production round."""

    def __init__(self, config_path: str | None = None, config: dict | None = None,
                 machine_id: str | None = None, min_interval_s: int | None = None,
                 exporter_runner: Callable | None = None,
                 submit_fn: Callable | None = None,
                 time_fn: Callable | None = None,
                 monotonic_fn: Callable | None = None) -> None:
        self.config_path = config_path
        self.config = config
        self.machine_id_override = machine_id
        if min_interval_s is None:
            self.min_interval_override: int | None = None
        else:
            try:
                self.min_interval_override = _clamp_interval(min_interval_s)
            except Exception:
                self.min_interval_override = DEFAULT_MIN_INTERVAL_S
        self.exporter_runner = exporter_runner
        self.submit_fn = submit_fn
        self.time_fn = time_fn or time.time
        self.monotonic_fn = monotonic_fn or time.monotonic
        self._last_step: float | None = None

    def _load_effective_config(self) -> tuple[dict | None, str | None, bool]:
        """Returns (config, error_class, disabled). Missing file => disabled."""
        if self.config is not None:
            if not isinstance(self.config, dict):
                return None, "invalid_config", False
            try:
                return validate_config(self.config), None, False
            except MetricsProducerError as exc:
                return None, str(exc), False
        path = resolve_config_path(self.config_path)
        try:
            raw = _load_config_file(path)
        except MetricsProducerError as exc:
            return None, str(exc), False
        if raw is None:
            return None, "disabled", True
        try:
            return validate_config(raw), None, False
        except MetricsProducerError as exc:
            return None, str(exc), False

    def _default_export_and_load(self, cfg: dict) -> tuple[dict | None, str | None]:
        from . import metrics as _metrics
        argv = _default_exporter_argv(cfg)
        # Confinement: argv[1:] paths must stay under /home when absolute.
        for token in argv[1:]:
            if isinstance(token, str) and token.startswith("/") and not token.startswith("/home/"):
                # Allow only the interpreter + /home paths + flag values.
                if token.startswith("-"):
                    continue
                return None, "exporter_failed"
        timeout_s = int(cfg.get("exporter_timeout_s", DEFAULT_EXPORTER_TIMEOUT_S))
        ok, fail = _run_exporter_subprocess(argv, timeout_s)
        if ok is None:
            return None, fail
        out_path = os.path.join(cfg["output_dir"], "metrics-snapshot.json")
        # Symlink/output confinement: real output must stay under real output_dir.
        try:
            cfg_real = os.path.realpath(cfg["output_dir"])
            # output file may not exist if exporter failed silently.
            if not os.path.isfile(out_path):
                return None, "exporter_failed"
            if not _real_under(out_path, cfg_real):
                return None, "exporter_failed"
        except OSError:
            return None, "exporter_failed"
        try:
            snapshot = _metrics.load_json_file(out_path)
        except Exception:
            # Map loader classes to minimal producer classes without echo.
            try:
                _metrics.load_json_file(out_path)
            except Exception as exc:
                name = type(exc).__name__
                if "MetricsError" in name:
                    return None, "invalid_snapshot"
                return None, "exporter_failed"
            return None, "exporter_failed"
        if not isinstance(snapshot, dict):
            return None, "invalid_snapshot"
        return snapshot, None

    def _check_freshness(self, snapshot: dict) -> str | None:
        from . import metrics as _metrics
        as_of_raw = snapshot.get("as_of") if isinstance(snapshot, dict) else None
        as_of = _metrics.parse_utc(as_of_raw)
        if as_of is None:
            return "invalid_snapshot"
        try:
            now = self.time_fn()
            as_of_ts = as_of.timestamp()
        except Exception:
            return "invalid_snapshot"
        age = now - as_of_ts
        if age < -FUTURE_SKEW_S:
            return "stale_snapshot"
        if age > STALE_AFTER_S:
            return "stale_snapshot"
        return None

    def step(self, force: bool = False, submit: bool = True) -> dict:
        now_mono = self.monotonic_fn()
        effective_min = self.min_interval_override
        # Peek config min when override absent and config file present? Use
        # override-or-default now; authoritative floor enforced after load.
        if effective_min is None:
            effective_min = DEFAULT_MIN_INTERVAL_S
        if not force and self._last_step is not None and effective_min > 0:
            if now_mono - self._last_step < effective_min:
                return {"machine_id": None, "observed_at": utc_now_iso(),
                        "as_of": None, "posted": False, "disabled": False,
                        "rate_limited": True, "errors": ["rate_limited"], "submit": None}
        self._last_step = now_mono

        cfg, err, disabled = self._load_effective_config()
        if disabled:
            return {"machine_id": None, "observed_at": utc_now_iso(),
                    "as_of": None, "posted": False, "disabled": True,
                    "rate_limited": False, "errors": ["disabled"], "submit": None}
        if cfg is None:
            return {"machine_id": None, "observed_at": utc_now_iso(),
                    "as_of": None, "posted": False, "disabled": False,
                    "rate_limited": False, "errors": [err or "invalid_config"], "submit": None}
        # Authoritative cadence: config floor wins unless override is stricter.
        cfg_min = int(cfg.get("min_interval_s", DEFAULT_MIN_INTERVAL_S))
        if self.min_interval_override is not None:
            cfg_min = max(cfg_min, self.min_interval_override)
        # Enforce re-check against actual elapsed using authoritative min:
        # (first call already stamped; second call rate-limits correctly).
        effective_min = cfg_min

        machine_id = self.machine_id_override or cfg.get("machine_id")
        if not machine_id or not isinstance(machine_id, str):
            return {"machine_id": None, "observed_at": utc_now_iso(),
                    "as_of": None, "posted": False, "disabled": False,
                    "rate_limited": False, "errors": ["missing_machine_id"], "submit": None}
        machine_id = str(machine_id)
        observed_at = utc_now_iso()
        errors: list = []

        # Export + project.
        snapshot: dict | None = None
        if self.exporter_runner is not None:
            try:
                snapshot = self.exporter_runner(cfg)
            except Exception as exc:
                errors.append("exporter_timeout" if type(exc).__name__ == "TimeoutError" else "exporter_failed")
                snapshot = None
        else:
            snapshot, load_err = self._default_export_and_load(cfg)
            if snapshot is None or load_err:
                errors.append(load_err or "exporter_failed")
                snapshot = None

        if snapshot is None:
            return {"machine_id": machine_id, "observed_at": observed_at,
                    "as_of": None, "posted": False, "disabled": False,
                    "rate_limited": False, "errors": errors or ["exporter_failed"], "submit": None}
        if not isinstance(snapshot, dict):
            return {"machine_id": machine_id, "observed_at": observed_at,
                    "as_of": None, "posted": False, "disabled": False,
                    "rate_limited": False, "errors": ["invalid_snapshot"], "submit": None}

        # Strict validation before any HTTP (no network on malformed).
        try:
            from . import metrics as _metrics
            _metrics.validate_snapshot_payload(snapshot, host_id=None)
        except Exception:
            return {"machine_id": machine_id, "observed_at": observed_at,
                    "as_of": None, "posted": False, "disabled": False,
                    "rate_limited": False, "errors": ["invalid_snapshot"], "submit": None}

        stale = self._check_freshness(snapshot)
        if stale:
            return {"machine_id": machine_id, "observed_at": observed_at,
                    "as_of": snapshot.get("as_of"), "posted": False, "disabled": False,
                    "rate_limited": False, "errors": [stale], "submit": None}

        if not submit:
            return {"machine_id": machine_id, "observed_at": observed_at,
                    "as_of": snapshot.get("as_of"), "posted": False, "disabled": False,
                    "rate_limited": False, "errors": [], "submit": None}

        # POST via existing dedicated host credential transport.
        try:
            fn = self.submit_fn
            if fn is None:
                from . import metrics as _metrics
                fn = _metrics.post_metrics_snapshot
            # post_metrics_snapshot(snapshot, base_url=None, token=None, timeout=...)
            # resolves base_url/token from host config (dedicated host credential).
            try:
                submit_result = fn(snapshot, timeout=float(cfg.get("post_timeout_s", DEFAULT_POST_TIMEOUT_S)))
            except TypeError:
                submit_result = fn(snapshot)
        except Exception:
            return {"machine_id": machine_id, "observed_at": observed_at,
                    "as_of": snapshot.get("as_of"), "posted": False, "disabled": False,
                    "rate_limited": False, "errors": ["submit_failed"], "submit": None}

        return {"machine_id": machine_id, "observed_at": observed_at,
                "as_of": snapshot.get("as_of"), "posted": True, "disabled": False,
                "rate_limited": False, "errors": [], "submit": submit_result}
