"""Shared, bounded-age snapshot of ``aplexer list --all --json`` and message logs.

Every host-side poller (fleet census, reservations, role dispatch, work claims,
status, adoption) used to spawn its own ``aplexer list`` subprocess. One snapshot
per executable now serves them all: at most one fetch per ``ttl`` seconds
(default 10 s, ``AGENT_HUB_NATIVE_LIST_TTL_S``), with concurrent readers
single-flighted onto the same fetch. Paths that act on state (claim re-qualify,
stop, reap confirmation, post-launch binding) pass ``max_age`` explicitly and get
a snapshot no older than that bound; effects (``start``/``kill``) invalidate.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

DEFAULT_TTL_S = 10.0
FRESH_MAX_AGE_S = 1.0  # bound for freshness-sensitive callers (coalesces bursts)
LOG_TTL_S = 3.0

_clock: Callable[[], float] = time.monotonic
_lock = threading.Lock()
_entries: dict[Any, dict[str, Any]] = {}


def ttl_s() -> float:
    try:
        return max(0.0, float(os.environ.get("AGENT_HUB_NATIVE_LIST_TTL_S", DEFAULT_TTL_S)))
    except ValueError:
        return DEFAULT_TTL_S


def _entry(key: Any) -> dict[str, Any]:
    with _lock:
        return _entries.setdefault(key, {"lock": threading.Lock(), "at": None, "value": None})


def cached(key: Any, fetch: Callable[[], Any], max_age: float) -> Any:
    """Return the snapshot for ``key`` no older than ``max_age`` s, fetching once if needed.

    Concurrent callers serialize on the entry lock; the first refreshes and the
    rest find a fresh-enough snapshot. A failed fetch is not cached.
    """
    entry = _entry(key)
    wanted_at = _clock()
    with entry["lock"]:
        at = entry["at"]
        if at is not None and (wanted_at - at <= max_age or at >= wanted_at):
            return entry["value"]
        started = _clock()
        value = fetch()
        entry["at"], entry["value"] = started, value
        return value


def invalidate(prefix: Any = None) -> None:
    with _lock:
        for key, entry in _entries.items():
            if prefix is None or (isinstance(key, tuple) and key[0] == prefix):
                entry["at"] = None


def list_raw(executable: str, fetch: Callable[[], Any], max_age: float | None = None) -> Any:
    """Shared ``list --all --json`` result; ``max_age`` None means the default TTL."""
    return cached(("list", executable), fetch, ttl_s() if max_age is None else min(max_age, ttl_s()))


def message_log(executable: str, workspace: str, fetch: Callable[[], Any], max_age: float | None = None) -> Any:
    return cached(("log", executable, workspace), fetch, LOG_TTL_S if max_age is None else max_age)
