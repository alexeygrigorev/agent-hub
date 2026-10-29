"""Live host-load admission signal (intake 0689ce9c AC2).

Launcher admission (``check_resources`` in the maintained launcher) and Hub
server placement (``src/cpu-headroom.ts``) are deliberately choking-only:
load average, busy% and predicted load NEVER refuse, and test processes
spawned INSIDE already-started sessions are never counted. On 2026-10-28
that let RMTHZ reach load 275-295 on 12 cores (46 vitest, 21 workerd,
25 playwright, 9 pytest from many sessions each running full suites) while
admission kept launching.

This module is the host-side counterweight: a live, fail-open load check
that runs in ``Dispatcher.dispatch`` BEFORE any admission subprocess,
claim or native start, and is ANDed into the heartbeat gate. Above
``factor`` (default 1.5x) times the core count, new launches are HELD
(offers stay queued, retried next cycle). Nothing running is ever touched:
no kills, no rejections that fail the launch, no server changes.

Readings come from ``os.getloadavg`` (1-minute average) and
``os.cpu_count``. Anything unreadable fails OPEN (no admission), matching
the existing telemetry philosophy; the status dict always says why.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Optional

#: Default overload factor: hold new launches above 1.5x cores (AC2).
HOST_LOAD_FACTOR = 1.5
#: Bounds for the operator ``host_load_factor`` settings override.
HOST_LOAD_FACTOR_MIN = 0.5
HOST_LOAD_FACTOR_MAX = 8.0
#: Fixed refusal slug prefix; the suffix carries bounded numbers only.
HOST_OVERLOAD_PREFIX = "host_overload"


def load_factor(settings: Optional[dict] = None) -> float:
    """Operator override for the overload factor; invalid falls back to default."""
    try:
        raw = (settings or {}).get("host_load_factor", HOST_LOAD_FACTOR)
        if isinstance(raw, bool):
            raise ValueError("bool factor")
        value = float(raw)
    except (TypeError, ValueError):
        return HOST_LOAD_FACTOR
    if not (HOST_LOAD_FACTOR_MIN <= value <= HOST_LOAD_FACTOR_MAX):
        return HOST_LOAD_FACTOR
    return value


def live_load_status(settings: Optional[dict] = None, *,
                     loadavg_fn: Optional[Callable[[], tuple]] = None,
                     cpu_count_fn: Optional[Callable[[], Optional[int]]] = None) -> dict[str, Any]:
    """Measure current 1-minute load against core count. Never raises.

    Returns ``{"load1": float|None, "cores": int|None, "factor": float,
    "overloaded": bool, "unknown": bool}``. ``overloaded`` is True only on
    a real reading above ``factor * cores``; every unreadable input fails
    open with ``unknown True``.
    """
    factor = load_factor(settings)
    try:
        getload = loadavg_fn or (os.getloadavg if hasattr(os, "getloadavg") else None)
        loads = getload() if getload else None
        load1 = float(loads[0]) if loads and len(loads) >= 1 else None
        if load1 is None or not (0 <= load1 <= 1_000_000):
            raise ValueError("unreadable load")
    except Exception:
        return {"load1": None, "cores": None, "factor": factor,
                "overloaded": False, "unknown": True}
    try:
        count = cpu_count_fn() if cpu_count_fn else os.cpu_count()
        cores = int(count) if count is not None else None
        if cores is None or cores < 1 or cores > 65536:
            raise ValueError("unreadable cores")
    except Exception:
        return {"load1": round(load1, 2), "cores": None, "factor": factor,
                "overloaded": False, "unknown": True}
    overloaded = load1 > factor * cores
    return {"load1": round(load1, 2), "cores": cores, "factor": factor,
            "overloaded": overloaded, "unknown": False}


def check_live_load(settings: Optional[dict] = None, **kwargs) -> dict[str, Any]:
    """Raise ``ValueError("host_overload: ...")`` when the host is overloaded.

    Returns the status dict when launches may proceed (including the
    fail-open unknown case). The message carries bounded numbers only, so
    it is safe to forward into diagnostics and Hub payloads.
    """
    status = live_load_status(settings, **kwargs)
    if status["overloaded"]:
        raise ValueError(
            f"{HOST_OVERLOAD_PREFIX}: load1 {status['load1']} above "
            f"{status['factor']}x{status['cores']} cores; launch held")
    return status


def gate_with_live_load(gate: dict[str, Any], settings: Optional[dict] = None,
                        **kwargs) -> dict[str, Any]:
    """AND the live-load signal into a heartbeat gate dict.

    An overloaded host reports ``launch_allowed False`` with a
    ``host_overload`` reason instead of masking the launcher verdict; a
    non-overloaded (or unreadable, fail-open) host keeps its gate
    untouched. Never raises; never mutates the input.
    """
    try:
        status = live_load_status(settings, **kwargs)
    except Exception:
        return gate
    if not status["overloaded"]:
        return gate
    out = dict(gate) if isinstance(gate, dict) else {}
    out["launch_allowed"] = False
    out["reason"] = (f"{HOST_OVERLOAD_PREFIX}: load1 {status['load1']} above "
                     f"{status['factor']}x{status['cores']} cores")
    return out
