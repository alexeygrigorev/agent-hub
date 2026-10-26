"""Host resource sampler + safe watchdog (intake 4fcd3139).

Per-session exact native process-tree accounting and a safe reaper.
Only Hub-managed, non-keep sessions are ever stopped. Human interactive
sessions (managed=False) are sampled at most, never reaped.

Server contract (see src/resource-management.ts, src/fleet.ts):
  GET /api/fleet/resources/watchdog -> {config, sessions:[...]}
  POST /api/fleet/resources/evidence {id,session_id,native_id,reason,output}
    (output UTF-8 BYTE limit 6000) -> {saved:true, evidence_id}
  POST /api/fleet/resources/report {samples:[...], reaps:[...]}
    -> {reported:true, ...} (the ONLY receipt acknowledgement)

Evidence is saved server-side as an intake note BEFORE any native stop, and
its exact {saved:true, evidence_id} confirmation is required before the
stop. After evidence, activity/output/incarnation AND server
keep/protection (via idempotent evidence re-POST) are requalified
immediately before EVERY stop for ALL reasons, against the full configured
idle threshold; any newer productive output refuses. Unknown state never
stops. Every stop captures a bounded redacted tail, stops cleanly, verifies
confirmed tree death, journals the pending intent BEFORE evidence (fail
closed when the journal cannot persist), reconciles uncertain stops
read-only without a second kill, replays unacknowledged receipts until the
server confirms {reported:true}, and reports the MEASURED tree release
(unknown stays unreported, never invented zero).

Stdlib only. Best-effort: any failure returns a summary, never raises
into Dispatcher.once.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

DEFAULT_WATCHDOG_INTERVAL_S = 300
DEFAULT_IDLE_MINUTES = 60
DEFAULT_RSS_FLAG_GB = 4

# Server bounds (src/resource-management.ts resourceConfig): host clamps to
# the same ranges so local gating never disagrees with Hub validation.
WATCHDOG_MIN_S = 300
WATCHDOG_MAX_S = 600
IDLE_MIN_MINUTES = 10
IDLE_MAX_MINUTES = 10080
RSS_FLAG_MIN_GB = 0.125
RSS_FLAG_MAX_GB = 1024

TERMINAL_SERVER_STATES = {"exited", "finished", "failed", "stopped", "dead", "refused"}
REAP_REASONS = ("idle", "terminal", "dead_pid")
# Server evidence/output limit is 6000 UTF-8 BYTES (intake note 8192 total).
EVIDENCE_MAX_BYTES = 6000
# Server request body cap is 32768 bytes; every report POST stays well under.
REPORT_BYTE_BUDGET = 24576
MAX_SAMPLES = 200
MAX_REAPS_PER_POST = 50
# Bound destructive work per cycle: few stops, bounded wall time, no scheduler.
MAX_REAPS_PER_CYCLE = 10
CYCLE_DEADLINE_S = 120.0

STATE_FILENAME = "resource-management.json"
STATE_CAP_BYTES = 256 * 1024
MAX_PEAK_ENTRIES = 1000
MAX_PENDING_ENTRIES = 200
MAX_REPORTED_ENTRIES = 200


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            f = float(value)
        except (ValueError, OverflowError):
            return None
        if f != f or f in (float("inf"), float("-inf")) or f < 0:
            return None
        return f
    return None


def _clamp(value: Any, lo: float, hi: float) -> Optional[float]:
    num = _finite_number(value)
    if num is None:
        return None
    return min(hi, max(lo, num))


def default_config() -> dict[str, Any]:
    return {
        "watchdog_interval_s": DEFAULT_WATCHDOG_INTERVAL_S,
        "idle_minutes": DEFAULT_IDLE_MINUTES,
        "rss_flag_gb": DEFAULT_RSS_FLAG_GB,
    }


def normalize_config(raw: Any) -> dict[str, Any]:
    cfg = default_config()
    if not isinstance(raw, dict):
        return cfg
    clamped = {
        "watchdog_interval_s": _clamp(raw.get("watchdog_interval_s"), WATCHDOG_MIN_S, WATCHDOG_MAX_S),
        "idle_minutes": _clamp(raw.get("idle_minutes"), IDLE_MIN_MINUTES, IDLE_MAX_MINUTES),
        "rss_flag_gb": _clamp(raw.get("rss_flag_gb"), RSS_FLAG_MIN_GB, RSS_FLAG_MAX_GB),
    }
    for key, val in clamped.items():
        if val is not None:
            cfg[key] = val
    return cfg


def watchdog_interval_ms(config: dict[str, Any]) -> int:
    """Destructive-pass cadence in ms, always within the server 300..600s window."""
    try:
        seconds = float(config.get("watchdog_interval_s", DEFAULT_WATCHDOG_INTERVAL_S))
    except (TypeError, ValueError):
        seconds = float(DEFAULT_WATCHDOG_INTERVAL_S)
    if seconds != seconds or seconds < WATCHDOG_MIN_S:
        seconds = float(WATCHDOG_MIN_S)
    if seconds > WATCHDOG_MAX_S:
        seconds = float(WATCHDOG_MAX_S)
    return int(seconds * 1000)


def _parse_iso_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    if type(value) is int and value > 0:
        # Already epoch ms (native rows).
        return value if value > 10_000_000_000 else None
    if isinstance(value, str) and value:
        try:
            text = value.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            import datetime as dt
            parsed = dt.datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return int(parsed.timestamp() * 1000)
        except (ValueError, OverflowError, OSError):
            return None
    return None


def _read_text(path: str, limit: int = 65536) -> Optional[str]:
    try:
        if not isinstance(path, str) or not path:
            return None
        if "\x00" in path:
            return None
        norm = os.path.normpath(path)
        # Callers validate cgroup containment (must be under /sys/fs/cgroup)
        # and proc roots separately; this reader only bounds size and errors.
        if not os.path.isabs(norm):
            return None
        with open(norm, "r", encoding="utf-8", errors="replace") as f:
            data = f.read(limit + 1)
        if len(data) > limit:
            return None
        return data
    except (OSError, ValueError):
        return None


def _parse_kv_int(text: str, key: str) -> Optional[int]:
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == key:
            try:
                return int(parts[1])
            except (ValueError, TypeError):
                return None
    return None


CGROUP_ROOT = "/sys/fs/cgroup"
# Shared scopes/slices that must never be sampled as one session's tree:
# the login session scopes, init, and anything that is not a dedicated
# workload scope leaf.
_SHARED_SCOPE_PREFIXES = ("session-", "user@")
_SHARED_SCOPE_NAMES = {"init.scope"}


def _cgroup_path(row_cgroup: Any, expect_pids: Any = None) -> Optional[str]:
    """Resolve one session's EXACT dedicated workload cgroup, else None.

    Accepts absolute ``/sys/fs/cgroup/...`` paths and the native
    ``/user.slice/...`` form (resolved under ``/sys/fs/cgroup``). Only an
    exact dedicated workload scope leaf (``*.scope``, not a shared
    user/session slice, service, or directory) is usable; anything else
    falls back to the exact workload process tree (caller-side). When
    ``expect_pids`` (worker/workload PIDs) are given and the scope's
    ``cgroup.procs`` is readable and non-empty yet contains none of them,
    the scope is foreign and rejected too. Never raises.
    """
    if not isinstance(row_cgroup, str) or not row_cgroup:
        return None
    text = row_cgroup
    if text.startswith("/user.slice/"):
        text = CGROUP_ROOT + text
    # Reject traversal before normalization (normpath would hide it).
    if ".." in text.split("/"):
        return None
    norm = os.path.normpath(text)
    if norm == CGROUP_ROOT or not norm.startswith(CGROUP_ROOT + "/"):
        return None
    if ".." in norm.split(os.sep):
        return None
    leaf = norm.rsplit("/", 1)[-1]
    if not leaf.endswith(".scope"):
        return None
    if leaf in _SHARED_SCOPE_NAMES or leaf.startswith(_SHARED_SCOPE_PREFIXES):
        return None
    if expect_pids is not None:
        try:
            wanted = {p for p in expect_pids if type(p) is int and p > 0}
        except TypeError:
            wanted = set()
        if wanted:
            members = _pids_in_cgroup(norm)
            if members and wanted.isdisjoint(members):
                return None
    return norm


def _pids_in_cgroup(cgroup: str) -> list[int]:
    out: list[int] = []
    try:
        raw = _read_text(os.path.join(cgroup, "cgroup.procs"), 1_000_000)
        if raw is None:
            return out
        for line in raw.splitlines()[:4096]:
            line = line.strip()
            if line.isdigit():
                pid = int(line)
                if 0 < pid < 2**22:
                    out.append(pid)
    except (OSError, ValueError):
        pass
    return out


def _smaps_totals(pid: int, proc_root: str = "/proc") -> Optional[tuple[float, float, float]]:
    """Return (rss_kb, pss_kb, anon_kb) for one pid, None when unreadable."""
    if type(pid) is not int or pid <= 0:
        return None
    text = _read_text(os.path.join(proc_root, str(pid), "smaps_rollup"), 65536)
    if text is None:
        return None
    rss = pss = anon = None
    for line in text.splitlines():
        if line.startswith("Rss:"):
            try:
                rss = float(line.split()[1])
            except (ValueError, IndexError):
                pass
        elif line.startswith("Pss:"):
            try:
                pss = float(line.split()[1])
            except (ValueError, IndexError):
                pass
        elif line.startswith("Anonymous:"):
            try:
                anon = float(line.split()[1])
            except (ValueError, IndexError):
                pass
    if rss is None and pss is None and anon is None:
        return None
    return (rss or 0.0, pss or 0.0, anon or 0.0)


def _stat_cpu_ticks(pid: int, proc_root: str = "/proc") -> Optional[int]:
    try:
        raw = _read_text(os.path.join(proc_root, str(pid), "stat"), 16384)
        if not raw:
            return None
        tail = raw.rsplit(")", 1)
        if len(tail) != 2:
            return None
        fields = tail[1].split()
        # fields[0] is state; utime=fields[11], stime=fields[12] after comm.
        utime = int(fields[11])
        stime = int(fields[12])
        if utime < 0 or stime < 0:
            return None
        return utime + stime
    except (OSError, ValueError, IndexError):
        return None


def _clk_tck() -> float:
    try:
        return float(os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, AttributeError):
        return 100.0


def _ppid(pid: int, proc_root: str = "/proc") -> Optional[int]:
    try:
        raw = _read_text(os.path.join(proc_root, str(pid), "stat"), 16384)
        if not raw:
            return None
        tail = raw.rsplit(")", 1)[1].split()
        return int(tail[1])
    except (OSError, ValueError, IndexError):
        return None


def collect_tree_pids(roots: list[int], proc_root: str = "/proc") -> list[int]:
    """Exact process-tree PIDs under roots via /proc PPid walk (bounded)."""
    valid = [p for p in roots if type(p) is int and p > 0]
    if not valid:
        return []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return list(valid)
    children: dict[int, list[int]] = {}
    for entry in entries[:32768]:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid <= 0 or pid >= 2**22:
            continue
        ppid = _ppid(pid, proc_root)
        if ppid is not None:
            children.setdefault(ppid, []).append(pid)
    seen = set(valid)
    queue = list(valid)
    while queue:
        parent = queue.pop()
        for child in children.get(parent, [])[:256]:
            if child not in seen:
                seen.add(child)
                queue.append(child)
                if len(seen) >= 1024:
                    return sorted(seen)
    return sorted(seen)


def sample_session_tree(
    native_row: Any,
    now_ms: int,
    proc_root: str = "/proc",
) -> Optional[dict[str, Any]]:
    """Sample one native session's exact tree. Returns MB/CPU dict or None.

    Prefers the kernel cgroup counters for the session's DEDICATED workload
    scope only (shared user/session slices are rejected by _cgroup_path and
    fall back to the exact process tree). RSS is cgroup ``anon`` (or the
    process-tree Anonymous sum) — ``memory.current``/``memory.peak`` include
    page cache and are never used as anon RSS. Unknown CPU/PSS stay None,
    never fabricated zero. Returns None only when no RSS signal exists.
    Never raises; unreadable sessions return None (unknown, never zero).
    """
    try:
        if isinstance(native_row, dict):
            cgroup = native_row.get("workload_cgroup") or native_row.get("containment_cgroup")
            worker_pid = native_row.get("worker_pid")
            workload_pid = native_row.get("workload_pid")
            created_at_ms = native_row.get("created_at_ms")
        else:
            cgroup = getattr(native_row, "workload_cgroup", None)
            worker_pid = getattr(native_row, "worker_pid", None)
            workload_pid = getattr(native_row, "workload_pid", None)
            created_at_ms = getattr(native_row, "created_at_ms", None)
        rss_mb = pss_mb = None
        cpu_seconds: Optional[float] = None
        roots = [p for p in (worker_pid, workload_pid) if type(p) is int and p > 0]
        cg = _cgroup_path(cgroup, roots or None)
        current_mb: Optional[float] = None
        if cg is not None:
            mem_stat = _read_text(os.path.join(cg, "memory.stat"))
            if mem_stat is not None:
                anon_bytes = _parse_kv_int(mem_stat, "anon")
                if anon_bytes is not None and anon_bytes > 0:
                    rss_mb = anon_bytes / (1024 * 1024)
            current_raw = _read_text(os.path.join(cg, "memory.current"))
            if current_raw is not None:
                try:
                    current_mb = int(current_raw.strip()) / (1024 * 1024)
                except (ValueError, AttributeError):
                    current_mb = None
            cpu_raw = _read_text(os.path.join(cg, "cpu.stat"))
            if cpu_raw is not None:
                usage = _parse_kv_int(cpu_raw, "usage_usec")
                if usage is not None and usage >= 0:
                    cpu_seconds = usage / 1_000_000.0
            # PSS from a bounded smaps sum over the scope's exact members
            # (plus the known workload roots when the scope lists none).
            pids = _pids_in_cgroup(cg)
            if not pids:
                pids.extend(roots)
            total_pss = 0.0
            counted = 0
            for pid in pids[:1024]:
                totals = _smaps_totals(pid, proc_root)
                if totals is None:
                    continue
                total_pss += totals[1]
                counted += 1
            if counted and total_pss > 0:
                pss_mb = total_pss / 1024.0
            if rss_mb is None and not counted:
                # No kernel anon and no readable tree members: unknown.
                pass
            if rss_mb is None and counted:
                # Scope anon unreadable but members readable: fall back to
                # the tree Anonymous sum (same basis as the proc-tree path).
                total_anon = 0.0
                total_rss = 0.0
                for pid in pids[:1024]:
                    totals = _smaps_totals(pid, proc_root)
                    if totals is not None:
                        total_anon += totals[2]
                        total_rss += totals[0]
                if total_anon > 0:
                    rss_mb = total_anon / 1024.0
                elif total_rss > 0:
                    rss_mb = total_rss / 1024.0
            if cpu_seconds is None and counted:
                total_ticks = 0
                ticked = False
                for pid in pids[:1024]:
                    ticks = _stat_cpu_ticks(pid, proc_root)
                    if ticks is not None:
                        total_ticks += ticks
                        ticked = True
                if ticked:
                    cpu_seconds = total_ticks / _clk_tck()
        else:
            if not roots:
                return None
            pids = collect_tree_pids(roots, proc_root)
            total_rss = total_pss = total_anon = 0.0
            total_ticks = 0
            counted = ticked = False
            for pid in pids[:1024]:
                totals = _smaps_totals(pid, proc_root)
                if totals is not None:
                    total_rss += totals[0]
                    total_pss += totals[1]
                    total_anon += totals[2]
                    counted = True
                ticks = _stat_cpu_ticks(pid, proc_root)
                if ticks is not None:
                    total_ticks += ticks
                    ticked = True
            if not counted:
                return None
            if total_anon > 0:
                rss_mb = total_anon / 1024.0
            elif total_rss > 0:
                rss_mb = total_rss / 1024.0
            else:
                return None
            if total_pss > 0:
                pss_mb = total_pss / 1024.0
            if ticked:
                cpu_seconds = total_ticks / _clk_tck()
        if rss_mb is None:
            return None
        runtime_s: Optional[float] = None
        if type(created_at_ms) is int and created_at_ms > 0 and now_ms > created_at_ms:
            runtime_s = (now_ms - created_at_ms) / 1000.0
        avg_cpu = None
        if cpu_seconds is not None and runtime_s and runtime_s > 0:
            avg_cpu = cpu_seconds / runtime_s
        return {
            "rss_mb": float(rss_mb),
            "pss_mb": float(pss_mb) if pss_mb is not None else None,
            "cpu_seconds": float(cpu_seconds) if cpu_seconds is not None else None,
            "runtime_s": float(runtime_s) if runtime_s is not None else None,
            "avg_cpu_cores": float(avg_cpu) if avg_cpu is not None else None,
            "current_mb": float(current_mb) if current_mb is not None else None,
        }
    except Exception:
        return None


def _redact(text: str) -> str:
    """Redact capture output. Fail closed: never return raw text."""
    try:
        from .workclaim import redact_for_bus
        redacted = redact_for_bus(text)
        if isinstance(redacted, str):
            return redacted
    except Exception:
        pass
    # Sanitizer failed or unavailable: capture is authorized private
    # evidence but must never carry potential secrets unredacted.
    return "[capture redaction unavailable]"


def _truncate_utf8_bytes(text: str, limit: int) -> str:
    """Truncate to a UTF-8 BYTE limit without splitting a code point."""
    if not isinstance(text, str):
        return ""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= limit:
        return text
    cut = raw[:limit]
    while cut:
        try:
            return cut.decode("utf-8")
        except UnicodeDecodeError:
            cut = cut[:-1]
    return ""


def _state_path(journal_dir: Any) -> Optional[Path]:
    if not journal_dir or not isinstance(journal_dir, str):
        return None
    try:
        base = Path(journal_dir)
        base.mkdir(parents=True, exist_ok=True)
        return base / STATE_FILENAME
    except (OSError, ValueError):
        return None


def load_state(journal_dir: Any) -> dict[str, Any]:
    path = _state_path(journal_dir)
    if path is None:
        return {"peaks": {}, "pending": {}, "reported": {}, "last_watchdog_ms": None}
    try:
        from . import private_fs
        raw = private_fs.read_bytes(str(path), STATE_CAP_BYTES)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            return {"peaks": {}, "pending": {}, "reported": {}, "last_watchdog_ms": None}
        peaks = data.get("peaks") if isinstance(data.get("peaks"), dict) else {}
        pending = data.get("pending") if isinstance(data.get("pending"), dict) else {}
        reported = data.get("reported") if isinstance(data.get("reported"), dict) else {}
        last_ms = data.get("last_watchdog_ms")
        # Bound and validate; reported receipts are durable until acked.
        clean_peaks = {}
        for key, val in list(peaks.items())[:MAX_PEAK_ENTRIES]:
            if isinstance(key, str) and isinstance(val, dict):
                clean_peaks[key] = val
        clean_pending = {}
        for key, val in list(pending.items())[:MAX_PENDING_ENTRIES]:
            if isinstance(key, str) and isinstance(val, dict):
                clean_pending[key] = val
        clean_reported = {}
        for key, val in list(reported.items())[:MAX_REPORTED_ENTRIES]:
            if isinstance(key, str) and isinstance(val, dict):
                clean_reported[key] = val
        return {"peaks": clean_peaks, "pending": clean_pending,
                "reported": clean_reported,
                "last_watchdog_ms": last_ms if type(last_ms) is int and last_ms > 0 else None}
    except (FileNotFoundError, OSError, ValueError):
        return {"peaks": {}, "pending": {}, "reported": {}, "last_watchdog_ms": None}


def _encode_state(state: dict[str, Any]) -> Optional[bytes]:
    """Serialize bounded state. Returns None when it cannot fit validly."""
    peaks = state.get("peaks") if isinstance(state.get("peaks"), dict) else {}
    pending = state.get("pending") if isinstance(state.get("pending"), dict) else {}
    reported = state.get("reported") if isinstance(state.get("reported"), dict) else {}
    last_ms = state.get("last_watchdog_ms")
    peaks = dict(list(peaks.items())[:MAX_PEAK_ENTRIES])
    pending = dict(list(pending.items())[:MAX_PENDING_ENTRIES])
    reported = dict(list(reported.items())[:MAX_REPORTED_ENTRIES])
    payload: dict[str, Any] = {"peaks": peaks, "pending": pending, "reported": reported}
    if type(last_ms) is int and last_ms > 0:
        payload["last_watchdog_ms"] = last_ms
    try:
        raw = json.dumps(payload).encode("utf-8")
    except (TypeError, ValueError):
        return None
    if len(raw) <= STATE_CAP_BYTES:
        return raw
    # Never write truncated JSON (corruption). Peaks are resampled every
    # cycle, so drop the stalest half and retry; pending intents and
    # unacknowledged reported receipts are never silently dropped here.
    items = list(peaks.items())
    items.sort(key=lambda kv: str((kv[1] or {}).get("observed_at") or ""))
    peaks = dict(items[len(items) // 2:])
    payload["peaks"] = peaks
    try:
        raw = json.dumps(payload).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return raw if len(raw) <= STATE_CAP_BYTES else None


def save_state(journal_dir: Any, state: dict[str, Any]) -> bool:
    """Atomically persist valid bounded JSON. True on success, False otherwise.

    Destructive stops must fail closed when this returns False. Never
    writes truncated/corrupt JSON and never swallows the outcome silently
    (the boolean is the signal).
    """
    path = _state_path(journal_dir)
    if path is None:
        return False
    try:
        from . import private_fs
        raw = _encode_state(state)
        if raw is None:
            return False
        private_fs.atomic_write(str(path), raw, STATE_CAP_BYTES)
        return True
    except Exception:
        return False


def _peak_key(session_id: str, native_id: str) -> str:
    return "%s\x00%s" % (session_id, native_id)


def update_peaks(state: dict[str, Any], session_id: str, native_id: str, sample: dict[str, Any], observed_at: str) -> dict[str, Any]:
    """Merge one sample into explicit high-water peaks (monotonic per field).

    Sample fields map EXPLICITLY onto stored peak fields: ``rss_mb`` feeds
    ``peak_rss_mb``, ``pss_mb`` feeds ``peak_pss_mb``; cumulative
    ``cpu_seconds`` takes the max. ``runtime_s``/``avg_cpu_cores``/
    ``current_mb`` are latest-value (callers freeze runtime for finished
    sessions before sampling). Unknown (None/non-finite) never clears a peak.
    """
    peaks = state.setdefault("peaks", {})
    if not isinstance(peaks, dict):
        peaks = state["peaks"] = {}
    key = _peak_key(session_id, native_id)
    prior = peaks.get(key) if isinstance(peaks.get(key), dict) else {}
    if not isinstance(prior, dict):
        prior = {}

    def _peak_max(prior_field: str, sample_field: str) -> Any:
        old_f = _finite_number(prior.get(prior_field))
        cur_f = _finite_number(sample.get(sample_field))
        if cur_f is None:
            return prior.get(prior_field)
        if old_f is None:
            return cur_f
        return max(cur_f, old_f)

    def _latest(field: str) -> Any:
        if _finite_number(sample.get(field)) is not None:
            return sample[field]
        return prior.get(field)

    peaks[key] = {
        "peak_rss_mb": _peak_max("peak_rss_mb", "rss_mb"),
        "peak_pss_mb": _peak_max("peak_pss_mb", "pss_mb"),
        "cpu_seconds": _peak_max("cpu_seconds", "cpu_seconds"),
        "runtime_s": _latest("runtime_s"),
        "avg_cpu_cores": _latest("avg_cpu_cores"),
        "current_mb": _latest("current_mb"),
        "observed_at": observed_at,
    }
    return peaks[key]


def _evidence_id(session_id: str, reason: str, created_ms: Any, activity_ms: Any, output_digest: str) -> str:
    """Per-attempt evidence id: session + reason + native incarnation +
    activity snapshot + output digest. A retry of the EXACT same attempt
    reuses the id (server-idempotent); any new activity/capture mints a new
    id so a stop never runs on a stale saved receipt."""
    safe_reason = "".join(c if (c.isalnum() or c in "-_:") else "-" for c in reason)[:32]
    try:
        created_part = str(int(created_ms))
    except (TypeError, ValueError):
        created_part = "0"
    try:
        activity_part = str(int(activity_ms))
    except (TypeError, ValueError):
        activity_part = "0"
    digest_part = "".join(c for c in str(output_digest) if c.isalnum())[:16]
    return ("%s:%s:%s:%s:%s" % (session_id, safe_reason, created_part, activity_part, digest_part))[:128]


def _reap_id(session_id: str, reason: str, created_ms: Any, output_digest: str) -> str:
    """Reap receipt id. Includes the native incarnation (a resumed session
    reusing a tag never collides) plus the attempt digest so each confirmed
    stop references its own evidence."""
    safe_reason = "".join(c if (c.isalnum() or c in "-_:") else "-" for c in reason)[:32]
    try:
        created_part = str(int(created_ms))
    except (TypeError, ValueError):
        created_part = "0"
    digest_part = "".join(c for c in str(output_digest) if c.isalnum())[:12]
    return ("%s:%s:%s:%s" % (session_id, created_part, safe_reason, digest_part))[:128]


def _output_digest(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def is_reapable_session(entry: Any) -> bool:
    """Never touch non-managed, keep, or unknown-state sessions."""
    if not isinstance(entry, dict):
        return False
    if not entry.get("managed"):
        return False
    if entry.get("keep"):
        return False
    for field in ("session_id", "native_id", "tag", "workspace"):
        if not isinstance(entry.get(field), str) or not entry[field]:
            return False
    if type(entry.get("native_created_at_ms")) is not int or entry["native_created_at_ms"] <= 0:
        return False
    state = entry.get("state")
    if not isinstance(state, str) or not state or state == "unknown":
        return False
    return True


def classify_reason(entry: dict[str, Any], native_phase: Optional[str], native_dead: Optional[bool], idle_ms: Optional[int], now_ms: int) -> Optional[str]:
    state = str(entry.get("state") or "")
    if state in TERMINAL_SERVER_STATES:
        return "terminal"
    if native_dead is True:
        return "dead_pid"
    if idle_ms is not None and idle_ms >= 0:
        last_ms = _parse_iso_ms(entry.get("last_activity_at"))
        if last_ms is None:
            # Fall back to started_at when no activity was ever recorded.
            last_ms = _parse_iso_ms(entry.get("started_at"))
        if last_ms is not None and now_ms - last_ms >= idle_ms:
            return "idle"
    _ = native_phase
    return None


def _native_dead(native: Any) -> Optional[bool]:
    """True when the owned workload is proven gone, False when alive, else None."""
    try:
        from .native_sessions import _confirm_workload_exit
        confirmed, _ = _confirm_workload_exit(native)
        # _confirm_workload_exit True means gone; False means alive-or-unknown.
        # Distinguish: check PIDs/cgroup directly for alive proof.
        if confirmed:
            return True
        pids = [p for p in (getattr(native, "worker_pid", None), getattr(native, "workload_pid", None)) if type(p) is int]
        if pids:
            from .native_sessions import _pid_dead
            states = [_pid_dead(p) for p in pids]
            if any(s is False for s in states):
                return False
            if states and all(s is True for s in states):
                return True
        if getattr(native, "containment_empty", None) is True:
            return True
        return None
    except Exception:
        return None


def _evidence_ack(response: Any, evidence_id: str) -> bool:
    """True only for the exact server confirmation {saved:true, evidence_id}."""
    return (isinstance(response, dict) and response.get("saved") is True
            and response.get("evidence_id") == evidence_id)


def _report_ack(response: Any) -> bool:
    """True only when the server confirms {reported:true}."""
    return isinstance(response, dict) and response.get("reported") is True


def _frozen_runtime_s(entry: dict[str, Any], prior: Any, measured: Optional[float]) -> Optional[float]:
    """Runtime for a sample row. Finished sessions freeze at
    finished_at - started_at (never advance with now); running sessions use
    the live measurement. A previously frozen peak is reused, never advanced."""
    if str(entry.get("state") or "") not in TERMINAL_SERVER_STATES:
        return measured
    finished_ms = _parse_iso_ms(entry.get("finished_at"))
    if finished_ms is not None:
        started_ms = _parse_iso_ms(entry.get("started_at"))
        base_ms = started_ms
        created_ms = entry.get("native_created_at_ms")
        if base_ms is None and type(created_ms) is int and created_ms > 0:
            base_ms = created_ms
        if base_ms is not None and finished_ms > base_ms:
            return (finished_ms - base_ms) / 1000.0
    prior_f = _finite_number(prior.get("runtime_s")) if isinstance(prior, dict) else None
    if prior_f is not None:
        return prior_f
    return measured


def _sample_row(sid: str, nid: str, peak: Any, observed_at: str) -> dict[str, Any]:
    """Server report row: unknown fields are omitted (null), never zeroed."""
    row: dict[str, Any] = {"session_id": sid, "native_id": nid, "observed_at": observed_at}
    if isinstance(peak, dict):
        for field in ("peak_rss_mb", "peak_pss_mb", "cpu_seconds", "runtime_s", "avg_cpu_cores"):
            if _finite_number(peak.get(field)) is not None:
                row[field] = peak[field]
    return row


def _valid_reap_dict(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    for field in ("id", "session_id", "native_id", "reason", "evidence_id", "at"):
        if not isinstance(value.get(field), str) or not value[field]:
            return None
    # freed_mb is nullable server-side: null means unproven, never zero.
    if value.get("freed_mb") is not None and _finite_number(value.get("freed_mb")) is None:
        return None
    if value.get("stopped") is not True:
        return None
    if value.get("reason") not in REAP_REASONS:
        return None
    return value


def _json_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value).encode("utf-8"))
    except (TypeError, ValueError):
        return 10**9


def _split_report_posts(samples: list[dict[str, Any]], reaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split samples+reaps into report POSTs each well under the server
    32768-byte body cap. Samples are idempotent max-merge (trimming only
    delays peaks); reaps are never dropped, only chunked."""
    base = len(b'{"samples":[],"reaps":[]}')
    posts: list[dict[str, Any]] = []
    cur_s: list[dict[str, Any]] = []
    cur_r: list[dict[str, Any]] = []

    def _bytes() -> int:
        return base + sum(_json_bytes(v) + 1 for v in cur_s) + sum(_json_bytes(v) + 1 for v in cur_r)

    def _flush() -> None:
        nonlocal cur_s, cur_r
        if cur_s or cur_r:
            posts.append({"samples": cur_s, "reaps": cur_r})
        cur_s = []
        cur_r = []
    for item in samples[:MAX_SAMPLES]:
        if (cur_s or cur_r) and _bytes() + _json_bytes(item) + 1 > REPORT_BYTE_BUDGET:
            _flush()
        cur_s.append(item)
    for item in reaps:
        if (cur_s or cur_r) and _bytes() + _json_bytes(item) + 1 > REPORT_BYTE_BUDGET:
            _flush()
        cur_r.append(item)
    _flush()
    return posts


def _build_reap(state: dict[str, Any], sid: str, nid: str, reason: str,
                reap_id: str, evidence_id: str, freed_mb: Optional[float], at_ms: int) -> dict[str, Any]:
    """Record a confirmed stop as an unacknowledged receipt (pending -> reported).

    The receipt stays in ``reported`` until a server ``{reported:true}``
    response; replays on future cycles carry the same idempotent id.
    ``freed_mb`` None means the release is unproven (server stores unknown),
    never a fabricated zero."""
    at_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at_ms / 1000))
    reap = {"id": reap_id, "session_id": sid, "native_id": nid, "reason": reason,
            "freed_mb": (round(float(freed_mb), 2) if freed_mb is not None else None),
            "at": at_iso, "evidence_id": evidence_id, "stopped": True}
    pending = state.get("pending")
    if isinstance(pending, dict):
        pending.pop(reap_id, None)
    reported = state.get("reported")
    if not isinstance(reported, dict):
        reported = state["reported"] = {}
    if reap_id in reported or len(reported) < MAX_REPORTED_ENTRIES:
        reported[reap_id] = reap
    return reap


def _reconcile_pending_oracles(aplexer_bin: str, state: dict[str, Any], clock: Any = None,
                               budget: int = 5) -> list[dict[str, Any]]:
    """Read-only recovery for prior uncertain stops. Never kills: each
    journaled oracle is confirmed via reconcile_stop_oracle against the SAME
    claim; confirmed stops become reported receipts (freed null when the
    release is unproven), everything else stays pending. Bounded per cycle."""
    out: list[dict[str, Any]] = []
    try:
        from .native_sessions import reconcile_stop_oracle
    except Exception:
        return out
    pending = state.get("pending")
    if not isinstance(pending, dict):
        return out
    for reap_id, item in list(pending.items())[:MAX_PENDING_ENTRIES]:
        if len(out) >= budget:
            break
        if not isinstance(item, dict) or not isinstance(item.get("oracle"), dict):
            continue
        kill = item.get("kill")
        try:
            confirmed, _why = reconcile_stop_oracle(
                aplexer_bin, item["oracle"], kill if isinstance(kill, dict) else {})
        except Exception:
            continue
        if not confirmed:
            continue
        before_mb = _finite_number(item.get("before_mb"))
        sid = item.get("session_id")
        nid = item.get("native_id")
        reason = item.get("reason")
        evidence_id = item.get("evidence_id")
        if not all(isinstance(v, str) and v for v in (reap_id, sid, nid, reason, evidence_id)):
            continue
        if reason not in REAP_REASONS:
            continue
        at_ms = clock() if callable(clock) else int(time.time() * 1000)
        out.append(_build_reap(state, sid, nid, reason, reap_id, evidence_id,
                               max(0.0, float(before_mb)) if before_mb is not None else None,
                               at_ms))
    return out


def _sweep_stale_pending(state: dict[str, Any], now_ms: int, max_age_ms: int = 24 * 3600 * 1000) -> None:
    """Drop aborted per-attempt intents older than a day. Entries carrying a
    stop oracle or a confirmed stop are durable evidence and are never swept;
    unacknowledged REPORTED receipts are only cleared by server ack."""
    pending = state.get("pending")
    if not isinstance(pending, dict):
        return
    for reap_id, item in list(pending.items()):
        if not isinstance(item, dict):
            pending.pop(reap_id, None)
            continue
        if item.get("oracle") is not None or item.get("stop_confirmed"):
            continue
        at_ms = item.get("at_ms")
        if type(at_ms) is int and now_ms - at_ms > max_age_ms:
            pending.pop(reap_id, None)


def _row_sample_dict(native: Any) -> dict[str, Any]:
    return {"workload_cgroup": getattr(native, "workload_cgroup", None),
            "worker_pid": getattr(native, "worker_pid", None),
            "workload_pid": getattr(native, "workload_pid", None),
            "created_at_ms": getattr(native, "created_at_ms", None)}


def run_watchdog_cycle(
    request: Any,
    runner: Any = None,
    journal_dir: Any = None,
    aplexer_bin: str = "aplexer",
    now_ms: Optional[int] = None,
    proc_root: str = "/proc",
    clock: Any = None,
) -> dict[str, Any]:
    """One best-effort watchdog pass. Never raises into the caller.

    ``now_ms`` stamps cycle samples; ``clock`` (optional callable returning
    epoch ms, default wall time) stamps post-stop ``reap.at`` with the actual
    stop time rather than a potentially stale cycle start."""
    summary: dict[str, Any] = {"samples": 0, "reaps": 0, "flagged": 0, "skipped": 0, "ok": True}
    try:
        now = int(now_ms) if type(now_ms) is int and now_ms > 0 else int(time.time() * 1000)
        try:
            watchdog = request("GET", "/api/fleet/resources/watchdog")
        except Exception as exc:
            summary.update({"ok": False, "error": type(exc).__name__[:64]})
            return summary
        if not isinstance(watchdog, dict):
            summary.update({"ok": False, "error": "invalid_watchdog"})
            return summary
        config = normalize_config(watchdog.get("config"))
        sessions = watchdog.get("sessions")
        if not isinstance(sessions, list):
            summary.update({"ok": False, "error": "invalid_watchdog_sessions"})
            return summary
        idle_ms = int(config["idle_minutes"] * 60 * 1000)
        rss_flag_mb = float(config["rss_flag_gb"]) * 1024.0

        try:
            from .native_sessions import list_sessions
            native_rows = list_sessions(aplexer_bin, include_exited=True)
        except Exception:
            native_rows = []
        by_native = {getattr(r, "session_id", "").lower(): r for r in native_rows if getattr(r, "session_id", None)}

        state = load_state(journal_dir)
        if not isinstance(state.get("peaks"), dict):
            state["peaks"] = {}
        _sweep_stale_pending(state, now)
        observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now / 1000))
        # Sample every heartbeat: live rows update peaks; terminal sessions
        # WITHOUT a current native row still emit cached/null final rows so
        # finished states are finalized durably instead of skipped.
        samples: list[dict[str, Any]] = []
        for entry in sessions[:500]:
            if not isinstance(entry, dict):
                continue
            sid = entry.get("session_id")
            nid = entry.get("native_id")
            if not isinstance(sid, str) or not isinstance(nid, str) or not sid or not nid:
                continue
            terminal = str(entry.get("state") or "") in TERMINAL_SERVER_STATES
            native = by_native.get(nid.lower())
            incarnation_ok = (
                native is not None
                and (type(entry.get("native_created_at_ms")) is not int
                     or getattr(native, "created_at_ms", None) == entry["native_created_at_ms"]))
            measured = sample_session_tree(_row_sample_dict(native), now, proc_root) if incarnation_ok else None
            if measured is not None:
                if terminal:
                    prior = state["peaks"].get(_peak_key(sid, nid))
                    frozen = _frozen_runtime_s(entry, prior, measured.get("runtime_s"))
                    measured["runtime_s"] = frozen
                    if frozen is not None and frozen > 0 and _finite_number(measured.get("cpu_seconds")) is not None:
                        measured["avg_cpu_cores"] = float(measured["cpu_seconds"]) / float(frozen)
                    else:
                        measured["avg_cpu_cores"] = None
                peak = update_peaks(state, sid, nid, measured, observed_at)
                row = _sample_row(sid, nid, peak, observed_at)
                # Fresh current anon RSS each heartbeat: the Hub credits ONLY
                # this against the launch ramp (never the peak). Cached final
                # rows below omit it, conservatively retaining the estimate.
                current_rss = _finite_number(measured.get("rss_mb"))
                if current_rss is not None:
                    row["current_rss_mb"] = float(current_rss)
                samples.append(row)
                if _finite_number(peak.get("peak_rss_mb")) is not None and peak["peak_rss_mb"] >= rss_flag_mb:
                    summary["flagged"] += 1
            elif terminal:
                # No current native row (or unreadable tree) for a finished
                # session: finalize from cached peaks, else null fields with
                # frozen runtime. Never skip finished states.
                prior = state["peaks"].get(_peak_key(sid, nid))
                view = dict(prior) if isinstance(prior, dict) else {}
                frozen = _frozen_runtime_s(entry, prior, None)
                if frozen is not None:
                    view["runtime_s"] = frozen
                samples.append(_sample_row(sid, nid, view, observed_at))

        # Destructive watchdog passes are gated to the configured 300..600s
        # cadence with a durable timestamp; sampling/reporting above runs
        # every heartbeat. No journal means no destructive stops (fail closed:
        # pending intents and oracle witnesses require durable state).
        reaps: list[dict[str, Any]] = []
        journal_ready = journal_dir is not None and _state_path(journal_dir) is not None
        last_pass = state.get("last_watchdog_ms")
        destructive_due = journal_ready and (
            type(last_pass) is not int or now - last_pass >= watchdog_interval_ms(config))
        reported_map = state.get("reported")
        if not isinstance(reported_map, dict):
            reported_map = state["reported"] = {}
        if destructive_due:
            # Recover prior uncertain stops read-only first (never re-kill).
            for recovered in _reconcile_pending_oracles(aplexer_bin, state, clock):
                if len(reaps) < MAX_REAPS_PER_CYCLE:
                    reaps.append(recovered)
            # Sessions already stop-confirmed this cycle (recovered) or with
            # an unacknowledged receipt (replay covers them) are never
            # re-evaluated for a fresh kill — binding-scoped, any attempt.
            settled = {(r["session_id"], r["native_id"]) for r in reaps
                       if isinstance(r, dict)}
            for cached in reported_map.values():
                valid = _valid_reap_dict(cached)
                if valid is not None:
                    settled.add((valid["session_id"], valid["native_id"]))
            destructive_ran = bool(reaps)
            deadline = time.monotonic() + CYCLE_DEADLINE_S
            if len(reaps) < MAX_REAPS_PER_CYCLE:
                for entry in sessions[:500]:
                    if len(reaps) >= MAX_REAPS_PER_CYCLE:
                        break
                    if time.monotonic() > deadline:
                        summary["skipped"] += 1
                        break
                    if not is_reapable_session(entry):
                        continue
                    sid = entry["session_id"]
                    nid = entry["native_id"]
                    native = by_native.get(nid.lower())
                    if native is None:
                        continue
                    if getattr(native, "created_at_ms", None) != entry["native_created_at_ms"]:
                        summary["skipped"] += 1
                        continue
                    phase = getattr(native, "phase", None)
                    if not isinstance(phase, str) or not phase or phase == "unknown":
                        summary["skipped"] += 1
                        continue
                    dead = _native_dead(native)
                    reason = classify_reason(entry, phase, dead, idle_ms, now)
                    if reason is None:
                        continue
                    if reason not in REAP_REASONS:
                        continue
                    if (sid, nid) in settled:
                        continue  # Stop already confirmed; the receipt replays.
                    destructive_ran = True
                    outcome = _reap_one(request, aplexer_bin, entry, native, reason,
                                        state, now, proc_root, idle_ms, journal_dir, clock)
                    if outcome is None:
                        summary["skipped"] += 1
                        continue
                    reaps.append(outcome)
            if destructive_ran:
                state["last_watchdog_ms"] = now
        if journal_ready:
            save_state(journal_dir, state)
        # Report samples + reaps (host auth via Dispatcher.request), replaying
        # unacknowledged receipts. Only a server {reported:true} clears them.
        reported_map = state.get("reported")
        if not isinstance(reported_map, dict):
            reported_map = state["reported"] = {}
        merged: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for cached in list(reported_map.values()):
            valid = _valid_reap_dict(cached)
            if valid is not None and valid["id"] not in seen_ids:
                seen_ids.add(valid["id"])
                merged.append(valid)
        for fresh_reap in reaps:
            if fresh_reap["id"] not in seen_ids:
                seen_ids.add(fresh_reap["id"])
                merged.append(fresh_reap)
        acked: set[str] = set()
        report_error: Optional[str] = None
        for post in _split_report_posts(samples, merged):
            try:
                response = request("POST", "/api/fleet/resources/report", post)
            except Exception as exc:
                report_error = type(exc).__name__[:64]
                break
            if not _report_ack(response):
                report_error = "report_not_acked"
                break
            for item in post["reaps"]:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    acked.add(item["id"])
        if report_error is not None:
            if samples or merged:
                summary.update({"ok": False, "error": report_error})
        else:
            for rid in acked:
                reported_map.pop(rid, None)
        for fresh_reap in reaps:
            if fresh_reap["id"] not in acked:
                if fresh_reap["id"] in reported_map or len(reported_map) < MAX_REPORTED_ENTRIES:
                    reported_map[fresh_reap["id"]] = fresh_reap
        if journal_ready:
            save_state(journal_dir, state)
        summary.update({"samples": len(samples), "reaps": len(reaps)})
        return summary
    except Exception as exc:  # Never break Dispatcher.once.
        summary.update({"ok": False, "error": type(exc).__name__[:64]})
        return summary


def _fresh_activity_ms(native: Any, entry: dict[str, Any]) -> Optional[int]:
    candidates = []
    last = getattr(native, "last_activity_ms", None)
    if type(last) is int and last > 0:
        candidates.append(last)
    parsed = _parse_iso_ms(entry.get("last_activity_at"))
    if parsed is not None:
        candidates.append(parsed)
    state_at = getattr(native, "agent_state_at_ms", None)
    if type(state_at) is int and state_at > 0:
        candidates.append(state_at)
    return max(candidates) if candidates else None


def _reap_one(request: Any, aplexer_bin: str, entry: dict[str, Any], native: Any, reason: str,
              state: dict[str, Any], now_ms: int, proc_root: str = "/proc",
              idle_ms: Optional[int] = None, journal_dir: Any = None,
              clock: Any = None) -> Optional[dict[str, Any]]:
    """Snapshot -> journal -> evidence -> REQUALIFY -> stop -> verify.

    Activity, output, and server keep/protection are requalified AFTER the
    successful evidence POST immediately before EVERY stop (all reasons):
    any newer productive output or activity refuses. The evidence response
    must be exactly {saved:true, evidence_id}. Evidence/reap ids are
    PER-ATTEMPT (incarnation + activity snapshot + output digest): a retry of
    the exact same attempt reuses the id, while new activity/capture mints a
    new id so a stop never runs on a stale saved receipt. The pending intent
    is journaled BEFORE evidence and the stop fails closed when the journal
    cannot be saved. Uncertain stops persist their oracle and reconcile
    read-only, never re-kill. Freed MB is the measured tree release after
    confirmed death; unproven stays null (never fabricated zero). ``reap.at``
    is the actual post-stop time via ``clock`` (or wall time), never the
    potentially stale cycle start. Returns the reap receipt or None to skip.
    """
    from .native_sessions import NativeTarget, SessionControlError, SessionUncertain
    from .native_sessions import capture_output, stop_session
    sid = entry["session_id"]
    nid = entry["native_id"]
    created_ms = entry.get("native_created_at_ms")
    pending = state.setdefault("pending", {})
    if not isinstance(pending, dict):
        pending = state["pending"] = {}
    # Reconcile-only path: a prior cycle left an uncertain-stop oracle for
    # this exact binding (any reason). Confirm read-only against the SAME
    # claim; while ANY attempt is uncertain there is NO fresh kill. A live
    # row or unproven exit stays pending.
    for known_id, prior in list(pending.items()):
        if not isinstance(prior, dict) or not isinstance(prior.get("oracle"), dict):
            continue
        if (prior.get("session_id") != sid or prior.get("native_id") != nid
                or prior.get("native_created_at_ms") != created_ms):
            continue
        try:
            from .native_sessions import reconcile_stop_oracle
            kill = prior.get("kill")
            confirmed, _why = reconcile_stop_oracle(
                aplexer_bin, prior["oracle"], kill if isinstance(kill, dict) else {})
        except Exception:
            return None
        if not confirmed:
            return None
        before_mb = _finite_number(prior.get("before_mb"))
        at_ms = clock() if callable(clock) else int(time.time() * 1000)
        stored_reason = prior.get("reason")
        stored_evidence = prior.get("evidence_id")
        if stored_reason not in REAP_REASONS or not isinstance(stored_evidence, str):
            return None
        return _build_reap(state, sid, nid, stored_reason, known_id, stored_evidence,
                           max(0.0, float(before_mb)) if before_mb is not None else None,
                           at_ms)
    if idle_ms is None or idle_ms < 0:
        return None  # Unknown threshold never stops.
    try:
        engine = entry.get("engine")
        target = NativeTarget(
            session_id=nid,
            tag=entry["tag"],
            workspace=entry["workspace"],
            created_at_ms=entry["native_created_at_ms"],
            engine=engine if isinstance(engine, str) else None,
        ).validated()
    except (SessionControlError, ValueError, TypeError):
        return None
    # Re-qualify immediately before any effect; races refuse.
    try:
        from .native_sessions import qualify_target
        fresh = qualify_target(aplexer_bin, target, live_only=False)
    except Exception:
        return None
    # Unknown state never stops.
    if not isinstance(getattr(fresh, "phase", None), str) or not fresh.phase or fresh.phase == "unknown":
        return None
    # Snapshot activity AND output BEFORE evidence. Unknown activity refuses;
    # a session already too active refuses before any server mutation.
    act0 = _fresh_activity_ms(fresh, entry)
    if act0 is None or now_ms - act0 < idle_ms:
        return None
    try:
        cap0 = capture_output(aplexer_bin, target, limit_bytes=EVIDENCE_MAX_BYTES, mode="tail")
        text0 = cap0.text if cap0 is not None and isinstance(cap0.text, str) else ""
    except Exception:
        # Capture failure is unknown output: refuse every stop.
        return None
    redacted = _truncate_utf8_bytes(_redact(text0), EVIDENCE_MAX_BYTES)
    digest = _output_digest(redacted)
    # Per-attempt ids: the exact same attempt reuses them (idempotent), while
    # any new activity/capture mints new ids (no stop on a stale receipt).
    evidence_id = _evidence_id(sid, reason, created_ms, act0, digest)
    reap_id = _reap_id(sid, reason, created_ms, digest)
    before = sample_session_tree(_row_sample_dict(fresh), now_ms, proc_root)
    before_mb: Optional[float] = None
    if before is not None:
        cand = before.get("current_mb") if _finite_number(before.get("current_mb")) is not None else before.get("rss_mb")
        before_mb = float(cand) if _finite_number(cand) is not None else None
    # Journal the pending intent (exact incarnation) BEFORE any server
    # mutation or native effect; fail closed when the journal cannot persist.
    pending[reap_id] = {"session_id": sid, "native_id": nid, "reason": reason,
                        "evidence_id": evidence_id, "at_ms": now_ms,
                        "native_created_at_ms": entry.get("native_created_at_ms"),
                        "tag": entry.get("tag"), "workspace": entry.get("workspace"),
                        "engine": entry.get("engine"),
                        "before_mb": before_mb}
    if not save_state(journal_dir, state):
        pending.pop(reap_id, None)
        return None
    evidence_payload = {"id": evidence_id, "session_id": sid, "native_id": nid,
                        "reason": reason, "output": redacted}
    try:
        response = request("POST", "/api/fleet/resources/evidence", evidence_payload)
    except Exception:
        return None
    if not _evidence_ack(response, evidence_id):
        return None
    # Post-evidence requalification for ALL reasons, immediately before the
    # stop: exact incarnation, no newer activity/output since the snapshot,
    # full configured idle threshold, and a server keep/protection recheck
    # via the idempotent evidence POST (same id re-runs session_not_reapable).
    try:
        fresh2 = qualify_target(aplexer_bin, target, live_only=False)
    except Exception:
        return None
    if not isinstance(getattr(fresh2, "phase", None), str) or not fresh2.phase or fresh2.phase == "unknown":
        return None
    act1 = _fresh_activity_ms(fresh2, entry)
    if act1 is None or act1 > act0 or now_ms - act1 < idle_ms:
        return None
    try:
        cap1 = capture_output(aplexer_bin, target, limit_bytes=EVIDENCE_MAX_BYTES, mode="tail")
        text1 = cap1.text if cap1 is not None and isinstance(cap1.text, str) else ""
    except Exception:
        return None
    if text1 != text0:
        # Newer productive output appeared during the evidence window:
        # an actively producing session (terminal-but-alive included) is
        # never stopped.
        return None
    try:
        recheck = request("POST", "/api/fleet/resources/evidence", evidence_payload)
    except Exception:
        return None
    if not _evidence_ack(recheck, evidence_id):
        return None
    # Clean native stop with exact incarnation + confirmed tree death.
    try:
        result = stop_session(aplexer_bin, target)
    except SessionUncertain as exc:
        oracle = getattr(exc, "oracle", None)
        kill = getattr(exc, "kill", None)
        if isinstance(oracle, dict):
            pending[reap_id]["oracle"] = oracle
            if isinstance(kill, dict):
                pending[reap_id]["kill"] = kill
            save_state(journal_dir, state)
            # Durable read-only reconciliation of the SAME claim, no re-kill.
            try:
                from .native_sessions import reconcile_stop_oracle
                confirmed, _why = reconcile_stop_oracle(
                    aplexer_bin, oracle, kill if isinstance(kill, dict) else {})
            except Exception:
                confirmed = False
            if confirmed:
                at_ms = clock() if callable(clock) else int(time.time() * 1000)
                return _build_reap(state, sid, nid, reason, reap_id, evidence_id,
                                   max(0.0, float(before_mb)) if before_mb is not None else None,
                                   at_ms)
        return None
    except Exception:
        return None
    if getattr(result, "state", None) != "stopped":
        return None
    # Freed MB is the ACTUAL measured tree release after confirmed death:
    # memory present before minus residual now (zero when the tree is gone).
    # Unproven stays null server-side (never a fabricated zero). ``at`` is
    # the actual post-stop time, not the stale cycle start.
    at_ms = clock() if callable(clock) else int(time.time() * 1000)
    freed: Optional[float] = None
    if before_mb is not None:
        residual_mb = 0.0
        try:
            after = sample_session_tree(_row_sample_dict(fresh2), now_ms, proc_root)
            if after is not None:
                for field in ("current_mb", "rss_mb"):
                    val = _finite_number(after.get(field))
                    if val is not None:
                        residual_mb = float(val)
                        break
        except Exception:
            residual_mb = 0.0
        freed = max(0.0, float(before_mb) - residual_mb)
    return _build_reap(state, sid, nid, reason, reap_id, evidence_id, freed, at_ms)
