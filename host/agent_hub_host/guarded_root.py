"""Guarded root bridge: reuses the exact flock and state.json contract of headless root.

Maintains strict compatibility with ~/.local/state/headless-root/root.lock and state.json:
- Single-instance guard via flock on root.lock.
- Healthy incumbent check: if heartbeat < 180s and age < 1500s + 120s and process alive, skip with note.
- Stale incumbent check: terminate previous process group, write superseded record, bump epoch.
- Lock-free previous crash check: record crashed superseded record.
- Coherent native epoch and Hub lease epoch binding in state.json.
- Periodic 60s heartbeat with fencing detection (abort if run_id / epoch altered).
- Completion summary and handoff markdown preservation.
"""

from __future__ import annotations

import datetime
try:
    import fcntl
except ImportError:
    fcntl = None
import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

WINDOW_SEC: int = 1500       # 25 min hard timeout
HB_STALE_SEC: int = 180     # Heartbeat older than 180s = stale
GRACE_SEC: int = 120        # Grace period before hard stale takeover
HEARTBEAT_INTERVAL: float = 60.0


def iso_ts(dt: Optional[datetime.datetime] = None) -> str:
    """Format aware ISO 8601 UTC timestamp."""
    d = dt or datetime.datetime.now(datetime.timezone.utc)
    return d.isoformat()


def get_current_pid_namespace() -> Optional[str]:
    """Return private PID namespace string if available."""
    try:
        return os.readlink("/proc/self/ns/pid")
    except Exception:
        return None


def get_pid_starttime_ticks(pid: int) -> Optional[int]:
    """Read process starttime in clock ticks from /proc/<pid>/stat to guard against PID reuse."""
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            content = f.read()
        rparen = content.rfind(")")
        if rparen == -1:
            return None
        fields = content[rparen + 2:].split()
        return int(fields[19])
    except Exception:
        return None


def get_process_uid(pid: int) -> Optional[int]:
    """Return UID of process owner from /proc/<pid>."""
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except Exception:
        return None


def is_pid_alive(pid: int) -> bool:
    """Check if process with given PID exists and belongs to current user without signaling."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        # Check proc ownership
        proc_stat = os.stat(f"/proc/{pid}")
        if proc_stat.st_uid != os.getuid():
            return False
        return True
    except (ProcessLookupError, PermissionError, FileNotFoundError, OSError):
        return False


class RootLockError(Exception):
    """Base error for root lock operations."""
    pass


class IncumbentStatus:
    HEALTHY = "healthy"
    STARTING = "starting"
    STALE = "stale"
    CRASHED = "crashed"
    NONE = "none"


class GuardedRootBridge:
    def __init__(
        self,
        state_dir: Optional[str] = None,
        clock: Optional[Any] = None,
        kill_handler: Optional[Callable[[int, int], None]] = None,
    ):
        """Initialize the root lock bridge.
        
        Args:
            state_dir: Directory containing root.lock, state.json, and runs/.
                       Defaults to $HR_STATE or ~/.local/state/headless-root.
            clock: Optional clock object providing time() and sleep().
            kill_handler: Optional handler for terminating process groups during takeover.
                          Defaults to safe standard signals. In unit tests this can be mocked.
        """
        env_state = os.environ.get("HR_STATE")
        if state_dir:
            self.state_dir = pathlib.Path(state_dir).resolve()
        elif env_state:
            self.state_dir = pathlib.Path(env_state).resolve()
        else:
            self.state_dir = pathlib.Path(os.path.expanduser("~/.local/state/headless-root")).resolve()

        canonical_path = pathlib.Path(os.path.expanduser("~/.local/state/headless-root")).resolve()
        try:
            import pwd
            passwd_home = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir).resolve() / ".local" / "state" / "headless-root"
        except Exception:
            passwd_home = canonical_path
        self.is_canonical_state: bool = (self.state_dir == canonical_path or self.state_dir == passwd_home)

        self.runs_dir = self.state_dir / "runs"
        self.lock_path = self.state_dir / "root.lock"
        self.state_file = self.state_dir / "state.json"
        self.log_file = self.state_dir / "root.log"

        self.clock = clock or time
        self.kill_handler = kill_handler or self._default_kill_pgid

        self._lock_fd: Optional[int] = None
        self._owned: bool = False
        self._run_id: Optional[str] = None
        self._epoch: int = 0
        self._hub_job_id: Optional[str] = None
        self._hub_lease_epoch: Optional[int] = None
        self._hub_url: Optional[str] = None

        self._hb_thread: Optional[threading.Thread] = None
        self._hb_stop_event = threading.Event()
        self._fenced: bool = False
        self._released: bool = False
        self._active_thread: Optional[threading.Thread] = None
        self._supervisor_proc: Optional[subprocess.Popen] = None

    def _iso_ts(self) -> str:
        """Format ISO 8601 UTC timestamp using injected clock."""
        try:
            clk_time = self.clock.time()
            return iso_ts(datetime.datetime.fromtimestamp(clk_time, tz=datetime.timezone.utc))
        except Exception:
            return iso_ts()

    def _ensure_dirs(self) -> None:
        if self.is_canonical_state:
            return
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def log(self, message: str) -> None:
        """Append log line with timestamp to root.log."""
        if self.is_canonical_state:
            return
        try:
            self._ensure_dirs()
            line = f"{self._iso_ts()} {message}\n"
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def is_lock_held(self) -> bool:
        """Check if root.lock is currently held by another process via read-only flock probe."""
        if self.is_canonical_state:
            return False
        if self._owned and self._lock_fd is not None:
            return True
        if not self.lock_path.exists():
            return False
        if fcntl is None:
            return False
        try:
            probe_fd = os.open(str(self.lock_path), os.O_RDONLY)
            try:
                fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(probe_fd, fcntl.LOCK_UN)
                return False
            except (BlockingIOError, OSError):
                return True
            finally:
                os.close(probe_fd)
        except FileNotFoundError:
            return False
        except (PermissionError, OSError):
            return True
        except Exception:
            return False

    def is_state_valid(self) -> Tuple[bool, Dict[str, Any]]:
        """Return (is_valid, state_dict). If state.json exists but is unreadable/corrupt, is_valid is False."""
        if self.is_canonical_state or not self.state_file.exists():
            return True, {}
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return True, data
                return False, {}
        except Exception:
            return False, {}

    def read_state(self) -> Dict[str, Any]:
        """Read state.json atomically."""
        if self.is_canonical_state or not self.state_file.exists():
            return {}
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def write_state(self, updates: Dict[str, Any]) -> None:
        """Atomic merge and write into state.json."""
        if self.is_canonical_state:
            return
        self._ensure_dirs()
        current = self.read_state()
        if "run_id" in updates and "hub_job_id" not in updates and "hub_job_id" in current:
            # Native run transition: clear stale hub fields
            current.pop("hub_job_id", None)
            current.pop("hub_lease_epoch", None)
            current.pop("hub_url", None)
        for k, v in updates.items():
            if v is None:
                current.pop(k, None)
            else:
                current[k] = v
        tmp_file = self.state_dir / f"state.json.tmp{os.getpid()}"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, self.state_file)

    def _default_kill_pgid(self, pgid: int, sig: int) -> bool:
        """Safely signal process group with rigorous ownership, namespace, and PID proof."""
        if self.is_canonical_state or pgid <= 1:
            return False
        # Never signal host agent daemon or its own process group, parent, or session leader
        try:
            my_pgid = os.getpgid(0)
            my_pid = os.getpid()
            my_ppid = os.getppid()
            my_sid = os.getsid(0)
            if pgid in (my_pgid, my_pid, my_ppid, my_sid):
                self.log(f"REFUSING signal: pgid {pgid} matches host agent daemon or caller pgid/pid/ppid/sid")
                return False
        except Exception:
            return False

        state = self.read_state()
        if state.get("pgid") != pgid:
            self.log(f"REFUSING signal: target pgid {pgid} does not match recorded state pgid {state.get('pgid')}")
            return False

        target_pid = state.get("pid", 0)
        if not isinstance(target_pid, int) or target_pid <= 1:
            return False

        # 0. Verify os.getpgid(target_pid) == pgid
        try:
            actual_pgid = os.getpgid(target_pid)
            if actual_pgid != pgid:
                self.log(f"REFUSING signal: target_pid {target_pid} pgid {actual_pgid} != expected pgid {pgid}")
                return False
        except (ProcessLookupError, PermissionError, OSError):
            return False

        # 1. Namespace check (enforce both current and target proc ns are readable and equal)
        current_ns = get_current_pid_namespace()
        rec_ns = state.get("pid_namespace")
        rec_ns_run = state.get("pid_namespace_run_id")
        rec_ns_epoch = state.get("pid_namespace_epoch")

        try:
            target_ns = os.readlink(f"/proc/{target_pid}/ns/pid")
        except Exception as e:
            self.log(f"REFUSING signal: cannot read target pid namespace: {e}")
            return False

        if not current_ns or not target_ns or target_ns != current_ns:
            self.log(f"REFUSING signal: target pid namespace mismatch target={target_ns} current={current_ns}")
            return False

        if rec_ns is not None:
            ns_bound = (rec_ns_run == state.get("run_id") and rec_ns_epoch == state.get("epoch"))
            if rec_ns_run is None and state.get("run_id") == self._run_id:
                ns_bound = True
            if ns_bound and rec_ns != current_ns:
                self.log(f"REFUSING signal: namespace mismatch recorded={rec_ns} current={current_ns}")
                return False

        # 2. UID check
        target_uid = get_process_uid(target_pid)
        if target_uid is None or target_uid != os.getuid():
            self.log(f"REFUSING signal: process {target_pid} UID mismatch or not found: {target_uid}")
            return False

        # 3. Starttime ticks check (protect against PID reuse, fail closed)
        current_ticks = get_pid_starttime_ticks(target_pid)
        if current_ticks is None:
            self.log(f"REFUSING signal: cannot read current starttime ticks for target_pid {target_pid}")
            return False

        rec_ticks = state.get("starttime_ticks")
        rec_ticks_epoch = state.get("starttime_ticks_epoch")
        rec_pid = state.get("starttime_ticks_pid")
        rec_run = state.get("starttime_ticks_run_id")

        if rec_ticks is not None:
            is_uncleaned_finished_legacy = (
                rec_run is None
                and rec_pid is None
                and rec_ticks_epoch is None
                and state.get("hub_job_id") is None
                and "end" in state
                and "result" in state
            )
            if not is_uncleaned_finished_legacy:
                if rec_run is not None and rec_run != state.get("run_id"):
                    self.log(f"REFUSING signal: starttime ticks run_id mismatch ({rec_run} vs {state.get('run_id')})")
                    return False
                if rec_pid is not None and rec_pid != target_pid:
                    self.log(f"REFUSING signal: starttime ticks pid mismatch ({rec_pid} vs {target_pid})")
                    return False
                if rec_ticks_epoch is not None and rec_ticks_epoch != state.get("epoch"):
                    self.log(f"REFUSING signal: starttime ticks epoch mismatch ({rec_ticks_epoch} vs {state.get('epoch')})")
                    return False
                if current_ticks != rec_ticks:
                    self.log(f"REFUSING signal: starttime ticks mismatch (PID reuse detected): recorded={rec_ticks}, current={current_ticks}")
                    return False

        # Safe to signal target root process group
        try:
            os.killpg(pgid, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def inspect_incumbent(self, new_run_id: str, lock_held: Optional[bool] = None) -> Tuple[str, Dict[str, Any]]:
        """Inspect incumbent state per the exact flock / state.json contract.
        
        Returns:
            Tuple of (IncumbentStatus, state_dict)
        """
        if self.is_canonical_state:
            return IncumbentStatus.NONE, {}

        if lock_held is None:
            lock_held = self.is_lock_held()

        is_valid, state = self.is_state_valid()
        if not is_valid:
            return (IncumbentStatus.STALE if lock_held else IncumbentStatus.CRASHED), {}

        prev_run = state.get("run_id")
        pst = state.get("status")
        ppid = state.get("pid", 0)
        pgid = state.get("pgid", 0)
        hb = state.get("heartbeat") if state.get("heartbeat") is not None else state.get("hb", 0)
        st = state.get("start") if state.get("start") is not None else state.get("st", 0)

        now_s = int(self.clock.time())
        try:
            hb = int(hb)
        except (ValueError, TypeError):
            hb = 0
        try:
            st = int(st)
        except (ValueError, TypeError):
            st = 0
        try:
            ppid = int(ppid)
        except (ValueError, TypeError):
            ppid = 0
        try:
            pgid = int(pgid)
        except (ValueError, TypeError):
            pgid = 0

        # Reject future timestamps as corrupt / anomaly
        if hb > now_s + 60 or st > now_s + 60 or (now_s - hb < -60) or (now_s - st < -60):
            return (IncumbentStatus.STALE if lock_held else IncumbentStatus.CRASHED), state

        hb_age = now_s - hb
        age = now_s - st

        if lock_held:
            # Lock is held: matching headless-root-run.sh line 59:
            # if [[ $pst != running ]] || { (( n - hb < HB_STALE && n - st < WINDOW + GRACE )) && kill -0 "${ppid_:-0}" 2>/dev/null; }; then
            if pst != "running":
                # Lock holder is starting up (state not yet written) or tearing down
                return IncumbentStatus.STARTING, state
            alive = is_pid_alive(ppid) if ppid > 0 else False
            if (0 <= hb_age < HB_STALE_SEC and 0 <= age < WINDOW_SEC + GRACE_SEC) and alive:
                return IncumbentStatus.HEALTHY, state
            else:
                return IncumbentStatus.STALE, state
        else:
            # Lock is free: matching headless-root-run.sh line 76-82:
            # prev=$(jget run_id); pst=$(jget status)
            # if [[ -n $prev && $pst == running ]]; then
            #   crashed
            if prev_run and pst == "running":
                return IncumbentStatus.CRASHED, state
            return IncumbentStatus.NONE, state

    def record_skip_note(self, prev_run_id: str, new_run_id: str, hb_age: int, age: int) -> None:
        """Append skip note to incumbent's inbox.md."""
        if self.is_canonical_state:
            return
        self._ensure_dirs()
        inbox_file = self.runs_dir / f"{prev_run_id}.inbox.md"
        note = f"- {self._iso_ts()} run {new_run_id} started and skipped: you were healthy (hb {hb_age}s old).\n"
        with open(inbox_file, "a", encoding="utf-8") as f:
            f.write(note)
        self.log(f"skip: previous root healthy run={prev_run_id} hb_age={hb_age}s age={age}s (new={new_run_id})")

    def record_superseded(self, prev_run_id: str, new_run_id: str, reason: str = "superseded") -> None:
        """Record superseded/crashed outcome for previous run."""
        if self.is_canonical_state:
            return
        self._ensure_dirs()
        superseded_file = self.runs_dir / f"{prev_run_id}.superseded.json"
        data = {
            "run_id": prev_run_id,
            "status": reason,
            "seen_by" if reason == "crashed" else "superseded_by": new_run_id,
            "at": self._iso_ts()
        }
        with open(superseded_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self.log(f"{reason} run={prev_run_id} by {new_run_id}")

    def acquire(
        self,
        hub_job_id: Optional[str] = None,
        hub_lease_epoch: Optional[int] = None,
        hub_url: Optional[str] = None,
        non_blocking: bool = True,
        allow_takeover: bool = True
    ) -> Dict[str, Any]:
        """Attempt to acquire the root lock according to real root contract.
        
        Returns:
            Dict containing:
                "acquired": bool
                "skip": bool
                "reason": str
                "run_id": str (if acquired)
                "epoch": int (if acquired)
        """
        if getattr(self, "_released", False):
            return {
                "acquired": False,
                "skip": True,
                "reason": "bridge_released"
            }

        self._active_thread = threading.current_thread()

        if self.is_canonical_state:
            return {
                "acquired": False,
                "skip": True,
                "reason": "canonical_root_state_forbidden_while_unsupported"
            }

        self._ensure_dirs()

        is_valid, state = self.is_state_valid()
        if not is_valid:
            self.log("acquire failed: state.json is corrupt")
            return {
                "acquired": False,
                "skip": False,
                "reason": "corrupt_state_json"
            }

        clk_time = self.clock.time()
        now_dt = datetime.datetime.fromtimestamp(clk_time, tz=datetime.timezone.utc)
        ts_str = now_dt.strftime('%Y%m%dT%H%M%S')
        GuardedRootBridge._run_seq = getattr(GuardedRootBridge, "_run_seq", 0) + 1
        new_run_id = f"{ts_str}-{os.getpid()}-{GuardedRootBridge._run_seq}"

        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        if fcntl is None:
            os.close(fd)
            raise RuntimeError("Guarded root bridge is not supported on Windows (requires POSIX fcntl)")
        flags = fcntl.LOCK_EX
        if non_blocking:
            flags |= fcntl.LOCK_NB

        locked = False
        try:
            fcntl.flock(fd, flags)
            locked = True
        except (BlockingIOError, OSError):
            locked = False

        pending_superseded: Optional[Tuple[str, str, str]] = None

        if not locked:
            # Lock is currently held by another process
            status, state = self.inspect_incumbent(new_run_id, lock_held=True)
            prev_run = state.get("run_id") or "starting"
            now_s = int(self.clock.time())
            hb = state.get("heartbeat") if state.get("heartbeat") is not None else state.get("hb", 0)
            st = state.get("start") if state.get("start") is not None else state.get("st", 0)
            hb_age = now_s - (int(hb) if isinstance(hb, (int, float)) else 0)
            age = now_s - (int(st) if isinstance(st, (int, float)) else 0)

            if status in (IncumbentStatus.HEALTHY, IncumbentStatus.STARTING) or not allow_takeover:
                self.record_skip_note(prev_run, new_run_id, hb_age, age)
                os.close(fd)
                return {
                    "acquired": False,
                    "skip": True,
                    "reason": f"previous root healthy run={prev_run} hb_age={hb_age}s",
                    "incumbent_run_id": prev_run,
                    "incumbent_epoch": state.get("epoch", 0)
                }

            # Stale incumbent -> Takeover
            pgid = state.get("pgid", 0)
            self.log(f"takeover: previous run={prev_run} stale (hb_age={hb_age}s age={age}s) pgid={pgid}; terminating")
            if pgid and int(pgid) > 1:
                self.kill_handler(int(pgid), signal.SIGTERM)
                # Give grace period to terminate
                deadline = self.clock.time() + 10.0
                while self.clock.time() < deadline:
                    if not is_pid_alive(state.get("pid", 0)):
                        break
                    self.clock.sleep(0.5)
                if is_pid_alive(state.get("pid", 0)):
                    self.kill_handler(int(pgid), signal.SIGKILL)

            # Wait for lock up to 30s
            deadline = self.clock.time() + 30.0
            while self.clock.time() < deadline:
                try:
                    if fcntl is None:
                        raise RuntimeError("Guarded root lock requires fcntl")
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except (BlockingIOError, OSError):
                    self.clock.sleep(0.5)

            if not locked:
                os.close(fd)
                self.log(f"takeover failed: lock still held after kill (new={new_run_id})")
                return {
                    "acquired": False,
                    "skip": False,
                    "reason": "takeover_lock_timeout",
                    "incumbent_run_id": prev_run
                }

            pending_superseded = (prev_run, new_run_id, "superseded")
        else:
            # Lock was free: check if previous run ended without finishing
            state = self.read_state()
            prev_run = state.get("run_id")
            if prev_run and state.get("status") == "running":
                self.log(f"previous run={prev_run} ended without finishing (crashed); handoff kept")
                pending_superseded = (prev_run, new_run_id, "crashed")

        # Now we own the lock
        self._lock_fd = fd
        self._owned = True
        self._run_id = new_run_id

        prev_state = self.read_state()
        prev_epoch = prev_state.get("epoch", 0)
        try:
            self._epoch = int(prev_epoch) + 1
        except (ValueError, TypeError):
            self._epoch = 1

        now_s = int(self.clock.time())
        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = "localhost"

        # Do not spawn a dummy sleep(1500) child process in acquire().
        self._supervisor_proc = None
        proc_pid = os.getpid()
        try:
            proc_pgid = os.getpgid(proc_pid)
        except Exception:
            proc_pgid = proc_pid

        if pending_superseded:
            self.record_superseded(*pending_superseded)

        current_ns = get_current_pid_namespace()
        proc_uid = get_process_uid(proc_pid) or os.getuid()

        self._hub_job_id = hub_job_id
        self._hub_lease_epoch = hub_lease_epoch
        self._hub_url = hub_url

        init_state = {
            "run_id": self._run_id,
            "pid": proc_pid,
            "pgid": proc_pgid,
            "pid_namespace": current_ns,
            "pid_namespace_run_id": self._run_id,
            "pid_namespace_epoch": self._epoch,
            "pid_namespace_pid": proc_pid,
            "starttime_ticks": None,
            "starttime_ticks_pid": None,
            "starttime_ticks_run_id": None,
            "starttime_ticks_epoch": None,
            "uid": proc_uid,
            "start": now_s,
            "heartbeat": now_s,
            "epoch": self._epoch,
            "status": "running",
            "prev_run": prev_state.get("run_id"),
            "host": hostname,
            "hub_job_id": hub_job_id,
            "hub_lease_epoch": hub_lease_epoch,
            "hub_url": hub_url
        }
        if hub_job_id is None:
            init_state.pop("hub_job_id", None)
            init_state.pop("hub_lease_epoch", None)
            init_state.pop("hub_url", None)
        self.write_state(init_state)
        self.log(f"start run={self._run_id} epoch={self._epoch} prev={prev_state.get('run_id', 'none')}")

        self._start_heartbeat()

        return {
            "acquired": True,
            "skip": False,
            "run_id": self._run_id,
            "epoch": self._epoch,
            "hub_job_id": hub_job_id,
            "hub_lease_epoch": hub_lease_epoch
        }

    def _start_heartbeat(self) -> None:
        """Start the background heartbeat thread."""
        self._hb_stop_event.clear()
        self._fenced = False

        def _loop():
            while not self._hb_stop_event.is_set():
                if self._hb_stop_event.wait(HEARTBEAT_INTERVAL):
                    break

                now_s = int(self.clock.time())
                current = self.read_state()
                start_s = current.get("start", now_s)

                if not isinstance(start_s, (int, float)):
                    self.log(f"fenced: corrupt start timestamp; stopping heartbeat run={self._run_id}")
                    self.fence("corrupt_start_timestamp")
                    break

                # Hardstop check: 25-minute limit (WINDOW_SEC)
                if now_s - start_s >= WINDOW_SEC:
                    self.log(f"hard-stopped-25m: run {self._run_id} reached 25m window limit")
                    self.fence("hard-stopped-25m")
                    break

                # Fencing check: verify state ownership
                if self.is_fenced():
                    self.log(f"fenced: state ownership changed; stopping heartbeat run={self._run_id}")
                    break

                self.write_state({"heartbeat": now_s, "hb": now_s})

        self._hb_thread = threading.Thread(target=_loop, name=f"root-hb-{self._run_id}", daemon=True)
        self._hb_thread.start()

    def fence(self, reason: str = "fenced") -> None:
        """Mark bridge as fenced and stop background supervisor/heartbeat."""
        self._fenced = True
        self.log(f"fence: {reason} run={self._run_id}")
        self._hb_stop_event.set()
        if self._supervisor_proc is not None:
            try:
                if self._supervisor_proc.poll() is None:
                    self._supervisor_proc.terminate()
                    try:
                        self._supervisor_proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        self._supervisor_proc.kill()
                        self._supervisor_proc.wait(timeout=0.5)
            except Exception:
                pass
        if self._lock_fd is not None:
            if fcntl is not None:
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                except Exception:
                    pass
            try:
                os.close(self._lock_fd)
            except Exception:
                pass
            self._lock_fd = None
            self._owned = False

    def is_fenced(self) -> bool:
        """Check if this run has been superseded or fenced in state.json or exceeded hardstop window."""
        if self._fenced:
            return True
        current = self.read_state()
        if self._run_id:
            now_s = int(self.clock.time())
            start_s = current.get("start", now_s)
            if not isinstance(start_s, (int, float)):
                self.fence("corrupt_start_timestamp")
                return True
            if now_s - start_s >= WINDOW_SEC:
                self.fence("25m_window_exceeded")
                return True
            if current.get("run_id") != self._run_id:
                self.fence("run_id_mismatch")
                return True
            elif current.get("epoch") is not None and current.get("epoch") != self._epoch:
                self.fence("epoch_mismatch")
                return True
            elif self._hub_lease_epoch is not None and current.get("hub_lease_epoch") != self._hub_lease_epoch:
                self.fence("hub_lease_epoch_mismatch")
                return True
            elif self._hub_job_id is not None and current.get("hub_job_id") != self._hub_job_id:
                self.fence("hub_job_id_mismatch")
                return True
        return self._fenced

    def finish(
        self,
        status: str = "ok",
        exit_code: int = 0,
        summary_text: str = "",
        agent_json: Optional[Dict[str, Any]] = None,
        evidence_refs: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Finalize the run, write handoff and summary artifacts, release flock."""
        if not self._owned or not self._run_id or getattr(self, "_released", False):
            return {"error": "not_owner"}

        # Stop heartbeat thread
        self._hb_stop_event.set()
        if self._hb_thread and self._hb_thread.is_alive():
            self._hb_thread.join(timeout=2.0)
            self._hb_thread = None

        # Stop dedicated supervisor process
        if self._supervisor_proc is not None:
            try:
                if self._supervisor_proc.poll() is None:
                    self._supervisor_proc.terminate()
                    try:
                        self._supervisor_proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        self._supervisor_proc.kill()
                        self._supervisor_proc.wait(timeout=1.0)
            except Exception:
                pass
            finally:
                self._supervisor_proc = None

        normalized_status = "ok" if status in ("succeeded", "ok") else status

        now_s = int(self.clock.time())
        state = self.read_state()
        start_s = state.get("start", now_s)
        prev_run = state.get("prev_run")

        handoff_path = self.runs_dir / f"{self._run_id}.handoff.md"
        summary_path = self.runs_dir / f"{self._run_id}.json"
        agent_path = self.runs_dir / f"{self._run_id}.agent.json"

        # Preserve disk agent.json if available
        if agent_json is None and agent_path.exists():
            try:
                with open(agent_path, "r", encoding="utf-8") as f:
                    agent_json = json.load(f)
            except Exception:
                agent_json = None
        elif agent_json is not None:
            with open(agent_path, "w", encoding="utf-8") as f:
                json.dump(agent_json, f, indent=2)

        # Write handoff markdown if not present
        if not handoff_path.exists() and summary_text:
            with open(handoff_path, "w", encoding="utf-8") as f:
                f.write(summary_text)

        # Write summary JSON
        summary_data = {
            "run_id": self._run_id,
            "epoch": self._epoch,
            "status": normalized_status,
            "exit_code": exit_code,
            "started": start_s,
            "ended": now_s,
            "duration_s": max(0, now_s - start_s),
            "prev_run": prev_run,
            "handoff": str(handoff_path) if handoff_path.exists() else None,
            "agent": agent_json,
            "hub_job_id": self._hub_job_id,
            "hub_lease_epoch": self._hub_lease_epoch,
            "evidence_refs": evidence_refs or []
        }
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary_data, f, indent=2)

        # Update state.json: clear stale Hub fields and bound process fields on finish
        current = self.read_state()
        if current.get("run_id") == self._run_id:
            self.write_state({
                "status": "finished",
                "end": now_s,
                "result": normalized_status,
                "hub_job_id": None,
                "hub_lease_epoch": None,
                "hub_url": None,
                "starttime_ticks": None,
                "starttime_ticks_pid": None,
                "starttime_ticks_run_id": None,
                "starttime_ticks_epoch": None,
                "pid_namespace": None,
                "pid_namespace_run_id": None,
                "pid_namespace_epoch": None,
                "pid_namespace_pid": None,
                "uid": None,
            })

        self.log(f"end run={self._run_id} status={normalized_status} rc={exit_code} dur={now_s - start_s}s summary={summary_path}")

        # Escalate error/timeout to NEEDS-ALEXEY.md
        if normalized_status in ("timeout", "error", "failed") and exit_code != 0:
            needs_alexey_path = self.state_dir / "NEEDS-ALEXEY.md"
            note = f"- {self._iso_ts()} headless root run {self._run_id} ended {normalized_status} (rc={exit_code}); see {self.log_file}\n"
            try:
                with open(needs_alexey_path, "a", encoding="utf-8") as f:
                    f.write(note)
            except Exception:
                pass

        # Release lock
        self.release()

        return summary_data

    def release(self) -> None:
        """Release the flock, terminate supervisor child, and stop heartbeat."""
        self._released = True
        self._hb_stop_event.set()
        if getattr(self, "_active_thread", None) and self._active_thread != threading.current_thread() and self._active_thread.is_alive():
            self._active_thread.join(timeout=0.5)
            self._active_thread = None
        if self._hb_thread and self._hb_thread.is_alive():
            self._hb_thread.join(timeout=2.0)
            self._hb_thread = None

        if self._run_id:
            try:
                curr = self.read_state()
                if curr.get("run_id") == self._run_id:
                    self.write_state({
                        "hub_job_id": None,
                        "hub_lease_epoch": None,
                        "hub_url": None,
                        "starttime_ticks": None,
                        "starttime_ticks_pid": None,
                        "starttime_ticks_run_id": None,
                        "starttime_ticks_epoch": None,
                        "pid_namespace": None,
                        "pid_namespace_run_id": None,
                        "pid_namespace_epoch": None,
                        "pid_namespace_pid": None,
                        "uid": None,
                    })
            except Exception:
                pass

        if self._supervisor_proc is not None:
            try:
                if self._supervisor_proc.poll() is None:
                    self._supervisor_proc.terminate()
                    try:
                        self._supervisor_proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        self._supervisor_proc.kill()
                        self._supervisor_proc.wait(timeout=1.0)
            except Exception:
                pass
            finally:
                self._supervisor_proc = None

        if self._lock_fd is not None:
            if fcntl is not None:
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                except Exception:
                    pass
            try:
                os.close(self._lock_fd)
            except Exception:
                pass
            self._lock_fd = None
        self._owned = False
        self._run_id = None
        self._epoch = 0
