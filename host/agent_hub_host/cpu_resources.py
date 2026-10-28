"""Real CPU telemetry and bounded, time-weighted lifetime session statistics."""
from __future__ import annotations

import math
from .host_oom import read_host_oom_delta
from pathlib import Path
import time
from datetime import datetime, timezone


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def sanitize_resource_choking(value, cooldown_s=300, now=None):
    """Only a bounded fresh server-owned hint, never task text or payload."""
    if not isinstance(value, dict) or set(value) != {"reason", "observed_at"}:
        return None
    if value.get("reason") not in {"host_oom", "host_runtime_choking"}:
        return None
    at = value.get("observed_at")
    if not isinstance(at, str) or len(at) > 64:
        return None
    try:
        observed = datetime.fromisoformat(at.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            return None
        if type(cooldown_s) not in (int, float) or not math.isfinite(cooldown_s) or not 30 <= cooldown_s <= 3600:
            cooldown_s = 300
        age = ((now or datetime.now(timezone.utc)) - observed).total_seconds()
        return dict(value) if -30 <= age <= cooldown_s else None
    except (TypeError, ValueError, OverflowError):
        return None


def sanitize_cpu_admission(value):
    """Fixed numeric refusal DTO. Arbitrary output never crosses this boundary."""
    required = {"expected_cpu_cores", "ramping_cpu_cores", "limit_cpu_cores", "target_utilization"}
    optional = {"current_cpu_cores", "psi_avg10", "psi_full_avg10", "memory_psi_full_avg10", "io_psi_full_avg10", "oom_kills_delta", "host_oom_kills_delta", "observed_at"}
    if not isinstance(value, dict) or not required.issubset(value) or set(value) - required - optional:
        return None
    for key, number in value.items():
        if key == "observed_at":
            if number is not None:
                try:
                    parsed = datetime.fromisoformat(number.replace("Z", "+00:00"))
                    if len(number) > 64 or parsed.tzinfo is None:
                        return None
                except (ValueError, AttributeError, TypeError):
                    return None
        elif number is None and key in optional:
            continue
        elif not _number(number) or number > (100 if "psi" in key else 1 if key == "target_utilization" else 1e9):
            return None
        elif key == "target_utilization" and number < .1:
            return None
        elif key.endswith("oom_kills_delta") and not isinstance(number, int):
            return None
    return dict(value)


def read_cpu_stat(proc_root="/proc"):
    """Exclude idle and iowait; guest counters are already included in user/nice."""
    try:
        lines = (Path(proc_root) / "stat").read_text().splitlines()
        fields = [int(n) for n in lines[0].split()[1:9]]
        cores = sum(line.split()[0][3:].isdigit() for line in lines if line.startswith("cpu"))
        if len(fields) != 8 or min(fields) < 0 or cores < 1 or not lines[0].startswith("cpu "):
            return None
        return {"ticks": fields, "cores": cores}
    except (OSError, ValueError, IndexError):
        return None


def read_cpu_psi(proc_root="/proc", resource="cpu", category="some"):
    try:
        for line in (Path(proc_root) / "pressure" / resource).read_text().splitlines():
            if line.startswith(category + " "):
                fields = dict(pair.split("=", 1) for pair in line.split()[1:])
                value = float(fields["avg10"])
                return value if _number(value) and value <= 100 else None
    except (OSError, ValueError, KeyError):
        pass
    return None


_last_oom_counter = None


def read_oom_delta(proc_root="/proc"):
    global _last_oom_counter
    try:
        values = dict(line.split() for line in (Path(proc_root) / "vmstat").read_text().splitlines())
        counter = int(values["oom_kill"])
        previous = _last_oom_counter
        _last_oom_counter = (proc_root, counter)
        if previous is None or previous[0] != proc_root or counter < previous[1]:
            return None
        return counter - previous[1]
    except (OSError, ValueError, KeyError):
        return None


def cpu_delta(before, after):
    if not before or not after or before["cores"] != after["cores"]:
        return None
    ticks = [b - a for a, b in zip(before["ticks"], after["ticks"])]
    total = sum(ticks)
    if min(ticks) < 0 or total <= 0:
        return None
    busy = total - ticks[3] - ticks[4]
    return {"busy_percent": busy / total * 100, "busy_cores": busy / total * after["cores"], "cores": after["cores"]}


def sample_cpu_usage(proc_root="/proc", interval_s=0.2, sleep=time.sleep, monotonic=time.monotonic, clock=time.time):
    before = read_cpu_stat(proc_root)
    started = monotonic()
    sleep(interval_s)
    after = read_cpu_stat(proc_root)
    elapsed = monotonic() - started
    result = cpu_delta(before, after)
    if result is None:
        result = {"busy_percent": None, "busy_cores": None, "cores": (after or before or {}).get("cores")}
    if not _number(elapsed) or elapsed <= 0:
        elapsed = None
    return {**result, "psi_avg10": read_cpu_psi(proc_root),
            "psi_full_avg10": read_cpu_psi(proc_root, category="full"),
            "memory_psi_full_avg10": read_cpu_psi(proc_root, resource="memory", category="full"),
            "io_psi_full_avg10": read_cpu_psi(proc_root, resource="io", category="full"),
            "oom_kills_delta": read_oom_delta(proc_root),
            "host_oom_kills_delta": read_host_oom_delta() if proc_root == "/proc" else None, "interval_s": elapsed,
            "observed_at": datetime.fromtimestamp(clock(), timezone.utc).isoformat().replace("+00:00", "Z")}


def update_cpu_distribution(state, counter_seconds, identity, observed_ms, *, terminal=False):
    """Track interval cores in 0.05-core upper buckets, weighted by elapsed time.

    The 20481 buckets cover up to 1024 cores; overflow uses the measured
    maximum. Persist this state with the existing per-incarnation peak journal.
    First/missing/reset/out-of-order observations cannot earn current CPU credit.
    A counter reset starts a new baseline while retaining prior measured intervals.
    """
    # A terminal observation is not another idle interval. Keep the lifetime
    # histogram/baseline unchanged even if an exited cgroup remains readable.
    frozen = terminal or state.get("cpu_finalized") is True
    if terminal:
        state["cpu_finalized"] = True
    current = None
    valid = not frozen and _number(counter_seconds) and _number(observed_ms) and isinstance(identity, str) and bool(identity)
    prior = state.get("cpu_baseline")
    interval_ms = None
    if valid and isinstance(prior, dict) and prior.get("identity") == identity:
        dt = observed_ms - prior["at"]
        dc = counter_seconds - prior["counter"]
        if dt > 0 and dc >= 0:
            current = dc / (dt / 1000)
            interval_ms = dt
            bucket = min(20480, math.ceil(current * 20))
            histogram = state.setdefault("cpu_histogram", {})
            histogram[str(bucket)] = histogram.get(str(bucket), 0) + dt
            state["cpu_max_cores"] = max(state.get("cpu_max_cores", 0), current)
    if valid and (not isinstance(prior, dict) or observed_ms > prior.get("at", -1)):
        state["cpu_baseline"] = {"identity": identity, "at": observed_ms, "counter": counter_seconds}
    histogram = state.get("cpu_histogram", {})
    total = sum(histogram.values())
    p95 = None
    accumulated = 0
    for key in sorted(histogram, key=int):
        accumulated += histogram[key]
        if accumulated >= total * .95:
            p95 = int(key) / 20 if int(key) < 20480 else max(1024, state["cpu_max_cores"])
            break
    return {"current_cpu_cores": current, "p95_cpu_cores": p95,
            "current_interval_ms": interval_ms}
