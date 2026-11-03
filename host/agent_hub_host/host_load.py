"""Live host-load admission signal (intake 0689ce9c AC2, reworked 2026-11-02).

Launcher admission (``check_resources`` in the maintained launcher) and Hub
server placement (``src/cpu-headroom.ts``) use the same bounded resource
policy. This host-side check enforces that policy again immediately before
claim, so a stale placement cannot bypass current load or memory pressure.

This module is the host-side counterweight: a live, fail-open check that runs
in ``Dispatcher.dispatch`` BEFORE any admission subprocess, claim or native
start, and is ANDed into the heartbeat gate. Nothing running is ever touched.

A host is busy only on a MEASURED signal (Alexey t2358u-t2363u):

* CPU busy >= 98% averaged over >= 10s (``/proc/stat`` deltas, iowait excluded);
* a 1-minute load average above ``factor`` (default 2) x cores;
* CPU-full PSI above its configured limit when that Linux signal is available;
* MemAvailable below 3% of RAM (at least 512 MiB, capped at 2 GiB); or
* swap use above its configured limit (default 80%).

Unreadable inputs fail OPEN (no refusal); the status dict always says why.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

#: Load cap as a multiple of cores. Hub policy can override this value.
HOST_LOAD_FACTOR = 2.0
#: Bounds for the operator ``host_load_factor`` settings override.
HOST_LOAD_FACTOR_MIN = 0.5
HOST_LOAD_FACTOR_MAX = 8.0
#: CPU is full only when measured busy CPU averages >= this over >= window.
CPU_SATURATED_PCT = 98.0
CPU_WINDOW_S = 10.0
#: Hold new launches below 3% of RAM (at least 512 MiB, capped at 2 GiB),
#: matching server placement's effectiveFloorMb (intake 189f6cd6).
MEM_FLOOR_BYTES = 2 * 1024 ** 3
MEM_FLOOR_MIN_BYTES = 512 * 1024 ** 2
MEM_FLOOR_PCT = 3
#: Fixed refusal slug prefix; the suffix carries bounded numbers only.
HOST_OVERLOAD_PREFIX = "host_overload"
_STATE_KEEP_S = 300.0


def load_factor(settings: Optional[dict] = None) -> float:
    """Operator override for the host-load factor; invalid falls back to default."""
    try:
        values = settings or {}
        raw = values.get("host_load_factor", values.get("cpu_load_factor", HOST_LOAD_FACTOR))
        if isinstance(raw, bool):
            raise ValueError("bool factor")
        value = float(raw)
    except (TypeError, ValueError):
        return HOST_LOAD_FACTOR
    if not (HOST_LOAD_FACTOR_MIN <= value <= HOST_LOAD_FACTOR_MAX):
        return HOST_LOAD_FACTOR
    return value


def policy_settings(settings: Optional[dict] = None, offer: Optional[dict] = None) -> dict:
    """Merge the local host override with the Hub's bounded resource policy."""
    result = dict(settings) if isinstance(settings, dict) else {}
    policy = result.get("resource_management")
    merged = dict(policy) if isinstance(policy, dict) else {}
    offered = offer.get("resource_management") if isinstance(offer, dict) else None
    if isinstance(offered, dict):
        merged.update(offered)
    if merged:
        result["resource_management"] = merged
        if "host_load_factor" not in result and "cpu_load_factor" in merged:
            result["host_load_factor"] = merged["cpu_load_factor"]
        for key in ("swap_max_used_percent", "cpu_psi_full_hard_cap_pct"):
            if key in merged:
                result[key] = merged[key]
    return result


def _state_path() -> Path:
    return Path(os.environ.get("AGENT_HUB_HOST_LOAD_STATE")
                or Path.home() / ".cache" / "agent-hub" / "host-load-cpu.json")


def sample_cpu_busy(*, read_fn: Optional[Callable[[], Optional[dict]]] = None,
                     sleep_fn: Callable[[float], None] = time.sleep,
                     clock_fn: Callable[[], float] = time.time,
                     state_path: Optional[Path] = None,
                     window_s: float = CPU_WINDOW_S) -> Optional[tuple]:
    """Average busy CPU percent over a window of >= ``window_s``; None if unknown.

    Returns ``(busy_percent, window_seconds)``. Snapshots persist across the
    host's one-shot cycles, so only the first call after a long gap sleeps
    (at most ``window_s``).
    """
    from . import cpu_resources
    read = read_fn or cpu_resources.read_cpu_stat
    path = state_path if state_path is not None else _state_path()
    try:
        now = clock_fn()
        current = read()
        if not current:
            return None
        history: list = []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            history = [{"at": float(h["at"]), "ticks": list(h["ticks"]), "cores": int(h["cores"])}
                       for h in raw if isinstance(h, dict) and 0 <= now - float(h["at"]) <= _STATE_KEEP_S]
        except (OSError, ValueError, KeyError, TypeError):
            history = []

        old = [h for h in history if now - h["at"] >= window_s]
        if old:
            base = max(old, key=lambda h: h["at"])
        else:
            if history:
                base = min(history, key=lambda h: h["at"])
                sleep_fn(max(0.0, window_s - (now - base["at"])))
            else:
                base = {"at": now, "ticks": current["ticks"], "cores": current["cores"]}
                history.append(base)
                sleep_fn(window_s)
            now = clock_fn()
            current = read()
            if not current:
                return None
        delta = cpu_resources.cpu_delta({"ticks": base["ticks"], "cores": base["cores"]}, current)
        history.append({"at": now, "ticks": current["ticks"], "cores": current["cores"]})
        keep = [h for h in history if now - h["at"] <= _STATE_KEEP_S][-12:]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(keep), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass
        if delta is None:
            return None
        return float(delta["busy_percent"]), round(now - base["at"], 1)
    except Exception:
        return None


def _default_mem() -> Optional[tuple]:
    try:
        from .host_memory import read_host_memory
        mem = read_host_memory()
        return (mem["available_bytes"], mem["total_bytes"]) if mem else None
    except Exception:
        return None


def _default_swap() -> Optional[float]:
    try:
        from .host_memory import read_host_swap_used_percent
        return read_host_swap_used_percent()
    except Exception:
        return None


def _default_cpu_psi_full() -> Optional[float]:
    try:
        from .cpu_resources import read_cpu_psi
        return read_cpu_psi(category="full")
    except Exception:
        return None


def live_load_status(settings: Optional[dict] = None, *,
                     loadavg_fn: Optional[Callable[[], tuple]] = None,
                     cpu_count_fn: Optional[Callable[[], Optional[int]]] = None,
                     cpu_busy_fn: Optional[Callable[[], Optional[tuple]]] = None,
                     mem_fn: Optional[Callable[[], Optional[tuple]]] = None,
                     swap_fn: Optional[Callable[[], Optional[float]]] = None,
                     cpu_psi_fn: Optional[Callable[[], Optional[float]]] = None) -> dict[str, Any]:
    """Measure the host. Never raises.

    Returns load1, cores, factor, CPU usage/window and MemAvailable/floor in
    measured units, plus ``overloaded``, the triggering signal, and whether
    all signals were unreadable.
    """
    settings = settings or {}
    factor = load_factor(settings)
    load1: Optional[float] = None
    cores: Optional[int] = None
    try:
        getload = loadavg_fn or (os.getloadavg if hasattr(os, "getloadavg") else None)
        loads = getload() if getload else None
        value = float(loads[0]) if loads and len(loads) >= 1 else None
        load1 = value if value is not None and 0 <= value <= 1_000_000 else None
    except Exception:
        load1 = None
    try:
        count = cpu_count_fn() if cpu_count_fn else os.cpu_count()
        cores = int(count) if count is not None else None
        if cores is not None and not (1 <= cores <= 65536):
            cores = None
    except Exception:
        cores = None

    runaway = load1 is not None and cores is not None and load1 > factor * cores
    cpu_busy = cpu_window = None
    if not runaway:
        try:
            measured = (cpu_busy_fn or measure_cpu_busy)()
            if measured and 0 <= measured[0] <= 100:
                cpu_busy, cpu_window = round(float(measured[0]), 1), measured[1]
        except Exception:
            pass
    mem_avail = mem_total = None
    try:
        mem = (mem_fn or _default_mem)()
        if mem:
            mem_avail, mem_total = int(mem[0]), int(mem[1])
    except Exception:
        pass

    swap_pct = None
    try:
        measured_swap = (swap_fn or _default_swap)()
        if measured_swap is not None and 0 <= float(measured_swap) <= 100:
            swap_pct = round(float(measured_swap), 1)
    except Exception:
        pass
    swap_limit = settings.get("swap_max_used_percent", 80)
    try:
        if isinstance(swap_limit, bool) or not 1 <= float(swap_limit) <= 100:
            swap_limit = 80
        else:
            swap_limit = float(swap_limit)
    except (TypeError, ValueError):
        swap_limit = 80

    psi_full = None
    try:
        measured_psi = (cpu_psi_fn or _default_cpu_psi_full)()
        if measured_psi is not None and 0 <= float(measured_psi) <= 100:
            psi_full = round(float(measured_psi), 2)
    except Exception:
        pass
    psi_limit = settings.get("cpu_psi_full_hard_cap_pct", 5)
    try:
        if isinstance(psi_limit, bool) or not 0.1 <= float(psi_limit) <= 100:
            psi_limit = 5
        else:
            psi_limit = float(psi_limit)
    except (TypeError, ValueError):
        psi_limit = 5

    mem_floor = None
    if mem_total and mem_total > 0:
        total_mb = mem_total / (1024 ** 2)
        floor_mb = min(
            MEM_FLOOR_BYTES // (1024 ** 2),
            max(MEM_FLOOR_MIN_BYTES // (1024 ** 2), round(total_mb * MEM_FLOOR_PCT / 100)),
        )
        mem_floor = int(floor_mb) * (1024 ** 2)
    trigger = None
    if runaway:
        trigger = "load_runaway"
    elif cpu_busy is not None and cpu_busy >= CPU_SATURATED_PCT:
        trigger = "cpu_busy"
    elif psi_full is not None and psi_full > psi_limit:
        trigger = "cpu_psi_full"
    elif mem_avail is not None and mem_floor is not None and mem_avail < mem_floor:
        trigger = "mem_available"
    elif swap_pct is not None and swap_pct > swap_limit:
        trigger = "swap_pressure"
    known = load1 is not None or cpu_busy is not None or mem_avail is not None or swap_pct is not None or psi_full is not None
    return {"load1": load1, "cores": cores, "factor": factor,
            "cpu_busy": cpu_busy, "cpu_window_s": cpu_window,
            "mem_avail_gb": None if mem_avail is None else round(mem_avail / 1024 ** 3, 1),
            "mem_avail_mb": None if mem_avail is None else mem_avail / 1024 ** 2,
            "mem_floor_mb": None if mem_floor is None else mem_floor / 1024 ** 2,
            "cpu_psi_full_pct": psi_full, "cpu_psi_full_limit_pct": psi_limit,
            "swap_used_pct": swap_pct, "swap_limit_pct": swap_limit,
            "trigger": trigger, "overloaded": trigger is not None, "unknown": not known}


def overload_reason(status: dict[str, Any]) -> str:
    """Bounded, numbers-only refusal text for an overloaded status."""
    trigger = status.get("trigger")
    if trigger == "cpu_busy":
        text = f"cpu busy {status.get('cpu_busy')}% over {status.get('cpu_window_s')}s"
    elif trigger == "cpu_psi_full":
        text = f"CPU full PSI {status.get('cpu_psi_full_pct')}% above {status.get('cpu_psi_full_limit_pct')}%"
    elif trigger == "mem_available":
        text = f"MemAvailable {status.get('mem_avail_gb')}G below floor"
    elif trigger == "swap_pressure":
        text = f"swap used {status.get('swap_used_pct')}% above {status.get('swap_limit_pct')}%"
    else:
        text = f"load1 {status.get('load1')} above {status.get('factor')}x{status.get('cores')} cores"
    return f"{HOST_OVERLOAD_PREFIX}: {text}"


def check_live_load(settings: Optional[dict] = None, **kwargs) -> dict[str, Any]:
    """Raise ``ValueError("host_overload: ...")`` when the host is overloaded."""
    status = live_load_status(settings, **kwargs)
    if status["overloaded"]:
        error = ValueError(f"{overload_reason(status)}; launch held")
        # Preserve the exact sample that caused the refusal. The dispatcher
        # uses it to report and persist a trigger-specific retry gate without
        # taking a second (potentially different) sample.
        error.host_load_status = status
        raise error
    return status


def hub_refusal(status: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return the Hub reason and bounded measurements for an overload."""
    trigger = status.get("trigger")
    if trigger == "mem_available":
        return "memory_pressure", {
            "trigger": trigger,
            "mem_avail_mb": status.get("mem_avail_mb"),
            "mem_floor_mb": status.get("mem_floor_mb"),
        }
    if trigger == "cpu_busy":
        return "cpu_pressure", {
            "trigger": trigger,
            "cpu_busy_pct": status.get("cpu_busy"),
            "cpu_window_s": status.get("cpu_window_s"),
        }
    if trigger == "cpu_psi_full":
        return "cpu_pressure", {
            "trigger": trigger,
            "cpu_psi_full_pct": status.get("cpu_psi_full_pct"),
            "cpu_psi_full_limit_pct": status.get("cpu_psi_full_limit_pct"),
        }
    if trigger == "swap_pressure":
        return "memory_pressure", {
            "trigger": trigger,
            "swap_used_pct": status.get("swap_used_pct"),
            "swap_limit_pct": status.get("swap_limit_pct"),
        }
    if trigger == "load_runaway":
        return "host_load_high", {
            "trigger": trigger,
            "load1": status.get("load1"),
            "cores": status.get("cores"),
            "factor": status.get("factor"),
        }
    raise ValueError("host overload status has no measured trigger")


def gate_with_live_load(gate: dict[str, Any], settings: Optional[dict] = None,
                        **kwargs) -> dict[str, Any]:
    """AND the live-load signal into a heartbeat gate dict (never raises/mutates)."""
    try:
        status = live_load_status(settings, **kwargs)
    except Exception:
        return gate
    if not status["overloaded"]:
        return gate
    out = dict(gate) if isinstance(gate, dict) else {}
    out["launch_allowed"] = False
    out["reason"] = overload_reason(status)
    return out
