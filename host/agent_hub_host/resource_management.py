"""Host resource sampler + safe watchdog (intake 4fcd3139).

Per-session exact native process-tree accounting and a safe reaper.
Only Hub-managed, non-keep sessions are ever stopped. Human interactive
sessions (managed=False) are sampled at most, never reaped.

Server contract (see src/resource-management.ts, src/fleet.ts):
  GET /api/fleet/resources/watchdog -> {config, sessions:[...]}
  POST /api/fleet/resources/evidence {id,session_id,native_id,reason,output}
  POST /api/fleet/resources/report {samples:[...], reaps:[...]}

Evidence is saved server-side as an intake note BEFORE any native stop.
Unknown state never stops. Every stop re-qualifies the exact
UUID/tag/workspace/generation via native_sessions, rechecks activity,
captures a bounded redacted tail, stops cleanly, verifies confirmed tree
death, journals retries with idempotent reap ids, and reports MB freed.

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

TERMINAL_SERVER_STATES = {"exited", "finished", "failed", "stopped", "dead", "refused"}
REAP_REASONS = ("idle", "terminal", "dead_pid")
MAX_EVIDENCE_CHARS = 16384
MAX_SAMPLES = 200
MAX_REAPS = 100

STATE_FILENAME = "resource-management.json"
STATE_CAP_BYTES = 256 * 1024


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
    for key in ("watchdog_interval_s", "idle_minutes", "rss_flag_gb"):
        val = _finite_number(raw.get(key))
        if val is not None and val > 0:
            cfg[key] = val
    return cfg


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


def _cgroup_path(row_cgroup: Any) -> Optional[str]:
    if not isinstance(row_cgroup, str) or not row_cgroup:
        return None
    norm = os.path.normpath(row_cgroup)
    if not norm.startswith("/sys/fs/cgroup/") or norm == "/sys/fs/cgroup" or norm == "/sys/fs/cgroup/":
        return None
    if ".." in norm.split(os.sep):
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

    Prefers the kernel cgroup counters (memory.stat anon, memory.current,
    memory.peak, cpu.stat usage_usec) which already cover the whole tree.
    Falls back to a bounded /proc smaps_rollup sum over the tree PIDs.
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
        rss_mb = pss_mb = anon_mb = None
        cpu_seconds: Optional[float] = None
        cg = _cgroup_path(cgroup)
        current_mb: Optional[float] = None
        if cg is not None:
            mem_stat = _read_text(os.path.join(cg, "memory.stat"))
            if mem_stat is not None:
                anon_bytes = _parse_kv_int(mem_stat, "anon")
                if anon_bytes is not None:
                    anon_mb = anon_bytes / (1024 * 1024)
            current_raw = _read_text(os.path.join(cg, "memory.current"))
            if current_raw is not None:
                try:
                    current_mb = int(current_raw.strip()) / (1024 * 1024)
                except (ValueError, AttributeError):
                    current_mb = None
            peak_raw = _read_text(os.path.join(cg, "memory.peak"))
            peak_mb: Optional[float] = None
            if peak_raw is not None:
                try:
                    peak_mb = int(peak_raw.strip()) / (1024 * 1024)
                except (ValueError, AttributeError):
                    peak_mb = None
            cpu_raw = _read_text(os.path.join(cg, "cpu.stat"))
            if cpu_raw is not None:
                usage = _parse_kv_int(cpu_raw, "usage_usec")
                if usage is not None:
                    cpu_seconds = usage / 1_000_000.0
            # Tree-exact RSS: cgroup current covers the tree; anon is the
            # placement-relevant high-water component.
            if anon_mb is not None:
                rss_mb = anon_mb
            elif current_mb is not None:
                rss_mb = current_mb
            if peak_mb is not None and rss_mb is not None:
                rss_mb = max(rss_mb, min(peak_mb, rss_mb) if False else rss_mb)
            # PSS from smaps sum when cgroup path is usable (bounded).
            pids = _pids_in_cgroup(cg)
            if not pids:
                for cand in (worker_pid, workload_pid):
                    if type(cand) is int and cand > 0:
                        pids.append(cand)
            total_rss = total_pss = total_anon = 0.0
            counted = 0
            for pid in pids[:1024]:
                totals = _smaps_totals(pid, proc_root)
                if totals is None:
                    continue
                total_rss += totals[0]
                total_pss += totals[1]
                total_anon += totals[2]
                counted += 1
            if counted:
                # Prefer kernel anon; fill PSS from the tree sum.
                pss_mb = total_pss / 1024.0
                if rss_mb is None:
                    rss_mb = total_anon / 1024.0 if total_anon else total_rss / 1024.0
        else:
            roots = [p for p in (worker_pid, workload_pid) if type(p) is int and p > 0]
            if not roots:
                return None
            pids = collect_tree_pids(roots, proc_root)
            total_rss = total_pss = total_anon = 0.0
            total_ticks = 0
            counted = 0
            for pid in pids[:1024]:
                totals = _smaps_totals(pid, proc_root)
                if totals is not None:
                    total_rss += totals[0]
                    total_pss += totals[1]
                    total_anon += totals[2]
                    counted += 1
                ticks = _stat_cpu_ticks(pid, proc_root)
                if ticks is not None:
                    total_ticks += ticks
            if not counted:
                return None
            rss_mb = (total_anon / 1024.0) if total_anon else (total_rss / 1024.0)
            pss_mb = total_pss / 1024.0
            if cpu_seconds is None:
                cpu_seconds = total_ticks / _clk_tck()
        if rss_mb is None or cpu_seconds is None:
            # Partial reads are unknown, never fabricated zero.
            if rss_mb is None:
                return None
            cpu_seconds = 0.0
        runtime_s: Optional[float] = None
        if type(created_at_ms) is int and created_at_ms > 0 and now_ms > created_at_ms:
            runtime_s = (now_ms - created_at_ms) / 1000.0
        avg_cpu = None
        if runtime_s and runtime_s > 0:
            avg_cpu = cpu_seconds / runtime_s
        return {
            "rss_mb": float(rss_mb),
            "pss_mb": float(pss_mb) if pss_mb is not None else None,
            "cpu_seconds": float(cpu_seconds),
            "runtime_s": float(runtime_s) if runtime_s is not None else None,
            "avg_cpu_cores": float(avg_cpu) if avg_cpu is not None else None,
            "current_mb": float(current_mb) if current_mb is not None else None,
        }
    except Exception:
        return None


def _redact(text: str) -> str:
    try:
        from .workclaim import redact_for_bus
        redacted = redact_for_bus(text)
        return redacted if isinstance(redacted, str) else text
    except Exception:
        return text


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
        return {"peaks": {}, "pending": {}}
    try:
        from . import private_fs
        raw = private_fs.read_bytes(str(path), STATE_CAP_BYTES)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            return {"peaks": {}, "pending": {}}
        peaks = data.get("peaks") if isinstance(data.get("peaks"), dict) else {}
        pending = data.get("pending") if isinstance(data.get("pending"), dict) else {}
        # Bound and validate.
        clean_peaks = {}
        for key, val in list(peaks.items())[:1000]:
            if isinstance(key, str) and isinstance(val, dict):
                clean_peaks[key] = val
        clean_pending = {}
        for key, val in list(pending.items())[:200]:
            if isinstance(key, str) and isinstance(val, dict):
                clean_pending[key] = val
        return {"peaks": clean_peaks, "pending": clean_pending}
    except (FileNotFoundError, OSError, ValueError):
        return {"peaks": {}, "pending": {}}


def save_state(journal_dir: Any, state: dict[str, Any]) -> None:
    path = _state_path(journal_dir)
    if path is None:
        return
    try:
        from . import private_fs
        raw = json.dumps({"peaks": state.get("peaks", {}), "pending": state.get("pending", {})}).encode("utf-8")
        private_fs.atomic_write(str(path), raw[:STATE_CAP_BYTES], STATE_CAP_BYTES)
    except Exception:
        pass


def _peak_key(session_id: str, native_id: str) -> str:
    return "%s\x00%s" % (session_id, native_id)


def update_peaks(state: dict[str, Any], session_id: str, native_id: str, sample: dict[str, Any], observed_at: str) -> dict[str, Any]:
    peaks = state.setdefault("peaks", {})
    key = _peak_key(session_id, native_id)
    prior = peaks.get(key) if isinstance(peaks.get(key), dict) else {}
    def _max(field: str) -> Any:
        cur = sample.get(field)
        old = prior.get(field)
        cur_f = _finite_number(cur)
        old_f = _finite_number(old)
        if cur_f is None:
            return old
        if old_f is None:
            return cur_f
        return max(cur_f, old_f)
    peaks[key] = {
        "peak_rss_mb": _max("rss_mb"),
        "peak_pss_mb": _max("pss_mb"),
        "cpu_seconds": _max("cpu_seconds"),
        "runtime_s": _max("runtime_s"),
        "avg_cpu_cores": sample.get("avg_cpu_cores") if _finite_number(sample.get("avg_cpu_cores")) is not None else prior.get("avg_cpu_cores"),
        "current_mb": sample.get("current_mb") if _finite_number(sample.get("current_mb")) is not None else prior.get("current_mb"),
        "observed_at": observed_at,
    }
    return peaks[key]


def _evidence_id(session_id: str, reason: str) -> str:
    safe_reason = "".join(c if (c.isalnum() or c in "-_:") else "-" for c in reason)[:32]
    return ("%s:%s" % (session_id, safe_reason))[:128]


def _reap_id(session_id: str, reason: str) -> str:
    return ("%s:%s:reap" % (session_id, reason))[:128]


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


def run_watchdog_cycle(
    request: Any,
    runner: Any = None,
    journal_dir: Any = None,
    aplexer_bin: str = "aplexer",
    now_ms: Optional[int] = None,
    proc_root: str = "/proc",
) -> dict[str, Any]:
    """One best-effort watchdog pass. Never raises into the caller."""
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
        observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now / 1000))
        samples: list[dict[str, Any]] = []
        for entry in sessions[:500]:
            if not isinstance(entry, dict):
                continue
            sid = entry.get("session_id")
            nid = entry.get("native_id")
            if not isinstance(sid, str) or not isinstance(nid, str) or not sid or not nid:
                continue
            native = by_native.get(nid.lower())
            if native is None:
                continue
            # Incarnation must match exactly; tag reuse never samples.
            if type(entry.get("native_created_at_ms")) is int and getattr(native, "created_at_ms", None) != entry["native_created_at_ms"]:
                continue
            measured = sample_session_tree(
                {"workload_cgroup": getattr(native, "workload_cgroup", None),
                 "worker_pid": getattr(native, "worker_pid", None),
                 "workload_pid": getattr(native, "workload_pid", None),
                 "created_at_ms": getattr(native, "created_at_ms", None)},
                now, proc_root)
            if measured is None:
                continue
            peak = update_peaks(state, sid, nid, measured, observed_at)
            sample: dict[str, Any] = {
                "session_id": sid,
                "native_id": nid,
                "observed_at": observed_at,
            }
            if _finite_number(peak.get("peak_rss_mb")) is not None:
                sample["peak_rss_mb"] = peak["peak_rss_mb"]
            if _finite_number(peak.get("peak_pss_mb")) is not None:
                sample["peak_pss_mb"] = peak["peak_pss_mb"]
            if _finite_number(peak.get("cpu_seconds")) is not None:
                sample["cpu_seconds"] = peak["cpu_seconds"]
            if _finite_number(peak.get("runtime_s")) is not None:
                sample["runtime_s"] = peak["runtime_s"]
            if _finite_number(peak.get("avg_cpu_cores")) is not None:
                sample["avg_cpu_cores"] = peak["avg_cpu_cores"]
            samples.append(sample)
            if _finite_number(peak.get("peak_rss_mb")) is not None and peak["peak_rss_mb"] >= rss_flag_mb:
                summary["flagged"] += 1
        samples = samples[:MAX_SAMPLES]

        # Select reap candidates with exact safety gates.
        reaps: list[dict[str, Any]] = []
        for entry in sessions[:500]:
            if len(reaps) >= MAX_REAPS:
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
            outcome = _reap_one(request, aplexer_bin, entry, native, reason, state, now, proc_root)
            if outcome is None:
                summary["skipped"] += 1
                continue
            reaps.append(outcome)
        save_state(journal_dir, state)
        # Report samples + reaps (host auth via Dispatcher.request).
        if samples or reaps:
            try:
                request("POST", "/api/fleet/resources/report", {"samples": samples, "reaps": reaps})
            except Exception as exc:
                summary.update({"ok": False, "error": type(exc).__name__[:64]})
                return summary
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


def _reap_one(request: Any, aplexer_bin: str, entry: dict[str, Any], native: Any, reason: str, state: dict[str, Any], now_ms: int, proc_root: str = "/proc") -> Optional[dict[str, Any]]:
    """Evidence -> recheck -> stop -> verify. Returns reap dict or None to skip."""
    from .native_sessions import NativeTarget, SessionControlError, capture_output, stop_session
    sid = entry["session_id"]
    nid = entry["native_id"]
    try:
        target = NativeTarget(
            session_id=nid,
            tag=entry["tag"],
            workspace=entry["workspace"],
            created_at_ms=entry["native_created_at_ms"],
            engine=entry.get("engine"),
        ).validated()
    except (SessionControlError, ValueError, TypeError):
        return None
    # Re-qualify immediately before any effect; races refuse.
    try:
        from .native_sessions import qualify_target
        fresh = qualify_target(aplexer_bin, target, live_only=False)
    except (SessionControlError, Exception):
        return None
    # Unknown state never stops.
    if not isinstance(getattr(fresh, "phase", None), str) or not fresh.phase or fresh.phase == "unknown":
        return None
    # Never stop active output: recheck activity against the fresh row.
    if reason == "idle":
        try:
            idle_ms_cfg = None
            # Re-derive idle from entry is not enough; use fresh native stamp.
            fresh_last = _fresh_activity_ms(fresh, entry)
            if fresh_last is None:
                return None
            # The entry threshold is enforced by the caller; here refuse any
            # session active within the last 60s regardless (output race).
            if now_ms - fresh_last < 60_000:
                return None
        except Exception:
            return None
    # Terminal sessions still need a live-or-recently-dead workload to stop;
    # an already-reaped tree reports freed 0 with evidence still required.
    # Measure memory before the stop for the freed-MB report.
    before = sample_session_tree(
        {"workload_cgroup": getattr(fresh, "workload_cgroup", None),
         "worker_pid": getattr(fresh, "worker_pid", None),
         "workload_pid": getattr(fresh, "workload_pid", None),
         "created_at_ms": getattr(fresh, "created_at_ms", None)},
        now_ms, proc_root)
    before_mb = None
    if before is not None:
        before_mb = before.get("current_mb") if _finite_number(before.get("current_mb")) is not None else before.get("rss_mb")
    # Bounded redacted final capture saved BEFORE the stop.
    try:
        captured = capture_output(aplexer_bin, target, limit_bytes=16384, mode="tail")
        output_text = captured.text if captured and isinstance(captured.text, str) else ""
    except (SessionControlError, Exception):
        # Capture failure is unknown output: refuse an idle stop, allow a
        # terminal/dead_pid stop to proceed only with empty redacted output?
        # Safest: refuse all stops when the final capture is unproven.
        return None
    redacted = _redact(output_text)[:MAX_EVIDENCE_CHARS]
    evidence_id = _evidence_id(sid, reason)
    reap_id = _reap_id(sid, reason)
    # Journal the pending intent before any server mutation (idempotent retry).
    pending = state.setdefault("pending", {})
    pending[reap_id] = {"session_id": sid, "native_id": nid, "reason": reason,
                        "evidence_id": evidence_id, "at_ms": now_ms}
    try:
        request("POST", "/api/fleet/resources/evidence",
                {"id": evidence_id, "session_id": sid, "native_id": nid,
                 "reason": reason, "output": redacted})
    except Exception:
        return None
    # Clean native stop with exact incarnation + confirmed tree death.
    try:
        result = stop_session(aplexer_bin, target)
    except Exception as exc:
        # Uncertain: oracle persisted on the exception; reconcile next cycle
        # via the journaled pending id (no second kill here).
        _ = getattr(exc, "oracle", None)
        return None
    if getattr(result, "state", None) != "stopped":
        return None
    freed = _finite_number(before_mb)
    freed_mb = round(float(freed), 2) if freed is not None else 0.0
    at_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ms / 1000))
    reap = {"id": reap_id, "session_id": sid, "native_id": nid, "reason": reason,
            "freed_mb": freed_mb, "at": at_iso, "evidence_id": evidence_id,
            "stopped": True}
    # Drop the pending marker only after a confirmed stop; the report POST
    # happens once per cycle with all reaps (idempotent server-side).
    pending.pop(reap_id, None)
    state.setdefault("reported", {})[reap_id] = reap
    return reap
