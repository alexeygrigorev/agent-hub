"""Real CPU telemetry and bounded, time-weighted lifetime session statistics."""
from __future__ import annotations

import math
from pathlib import Path
import time
from datetime import datetime, timezone


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


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


def read_cpu_psi(proc_root="/proc"):
    try:
        for line in (Path(proc_root) / "pressure/cpu").read_text().splitlines():
            if line.startswith("some "):
                fields = dict(pair.split("=", 1) for pair in line.split()[1:])
                value = float(fields["avg10"])
                return value if _number(value) and value <= 100 else None
    except (OSError, ValueError, KeyError):
        pass
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
    if result is None or not _number(elapsed) or elapsed <= 0:
        return None
    return {**result, "psi_avg10": read_cpu_psi(proc_root), "interval_s": elapsed,
            "observed_at": datetime.fromtimestamp(clock(), timezone.utc).isoformat().replace("+00:00", "Z")}


def update_cpu_distribution(state, counter_seconds, identity, observed_ms):
    """Track interval cores in 0.05-core upper buckets, weighted by elapsed time.

    The 20481 buckets cover up to 1024 cores; overflow uses the measured
    maximum. Persist this state with the existing per-incarnation peak journal.
    First/missing/reset/out-of-order observations cannot earn current CPU credit.
    A counter reset starts a new baseline while retaining prior measured intervals.
    """
    current = None
    valid = _number(counter_seconds) and _number(observed_ms) and isinstance(identity, str) and bool(identity)
    prior = state.get("cpu_baseline")
    if valid and isinstance(prior, dict) and prior.get("identity") == identity:
        dt = observed_ms - prior["at"]
        dc = counter_seconds - prior["counter"]
        if dt > 0 and dc >= 0:
            current = dc / (dt / 1000)
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
    return {"current_cpu_cores": current, "p95_cpu_cores": p95}
