"""Orphan prevention: stop headless sessions whose worktree is gone.

Detects sessions whose workspace/worktree directory no longer exists
(cwd deleted, i.e. /proc/<pid>/cwd ends in "(deleted)" or the path is
missing), after a configurable grace period (default 2h). Stops them via
the normal Hub/aplexer stop path (graceful HUP, then SIGKILL escalation),
records reason worktree_missing on the Hub session via
`hub sessions observe --worktree-missing` (linked-intake note is best-effort
operator follow-up when Hub credentials permit).

Interactive sessions Alexey started manually are never stopped; they are
reported only. Human ownership is determined from Hub session info
(human_owned / protected) or, when Hub is unreachable, conservatively from
the native tag (non hub-/task-hub- tags are treated as interactive).

Stdlib only. All external calls are argv arrays (no shell).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

DEFAULT_GRACE_HOURS = 2.0
STATE_VERSION = 1

SESSION_ID_RE = re.compile(r"[0-9a-fA-F-]{8,64}\Z")
TAG_HEADLESS_RE = re.compile(r"^(hub-|task-hub-)")


def _strip_deleted_suffix(path: str) -> str:
    if isinstance(path, str) and path.endswith(" (deleted)") and not os.path.exists(path):
        return path[: -len(" (deleted)")]
    return path


def workspace_missing(workspace: str | None) -> bool:
    """True when the workspace path is absent. Unknown -> False (fail-closed)."""
    if not isinstance(workspace, str) or not workspace:
        return False
    cand = workspace
    if cand.endswith(" (deleted)"):
        # Kernel annotation: the session's original directory is gone unless a
        # literal same-named path exists (fleet.session_metadata semantics).
        try:
            return not os.path.exists(cand)
        except Exception:
            return True
    try:
        return not os.path.isdir(cand)
    except Exception:
        return True


def proc_cwd_deleted(pid: Any, readlink: Callable[[str], str] | None = None) -> bool | None:
    """Check /proc/<pid>/cwd for a '(deleted)' suffix.

    Returns True (deleted), False (present), None (unknown/unreadable).
    """
    if type(pid) is not int or pid <= 0:
        return None
    link = f"/proc/{pid}/cwd"
    try:
        target = readlink(link) if readlink else os.readlink(link)
    except Exception:
        return None
    return isinstance(target, str) and target.endswith(" (deleted)")


def native_missing(row: dict) -> bool:
    """True when workspace/cwd evidence proves the tree is gone. Unknown -> False."""
    ws = row.get("workspace")
    cwd = row.get("cwd")
    if isinstance(ws, str) and ws and workspace_missing(ws):
        return True
    if isinstance(cwd, str) and cwd.endswith(" (deleted)") and not os.path.exists(cwd):
        return True
    for key in ("workload_pid", "worker_pid"):
        pid = row.get(key)
        deleted = proc_cwd_deleted(pid)
        if deleted is True:
            return True
    return False


def is_interactive(row: dict, hub_info: dict | None) -> bool:
    """Interactive/manual sessions are report-only, never stopped."""
    if isinstance(hub_info, dict):
        if hub_info.get("human_owned") is True or hub_info.get("protected") is True:
            return True
        # Server canonical: starter admin/human or human_owned flag.
        starter = hub_info.get("starter")
        if isinstance(starter, str) and (starter == "admin" or starter.startswith("human:")):
            return True
        mode = hub_info.get("mode") or hub_info.get("session_type")
        if isinstance(mode, str) and mode.lower() == "interactive":
            return True
    tag = row.get("tag") if isinstance(row.get("tag"), str) else ""
    # Headless Hub launches carry hub-<id> / task-hub-<id> tags. Anything
    # else, including empty/unknown tags, is conservatively manual/report-only.
    if not tag or not TAG_HEADLESS_RE.match(tag):
        return True
    return False


def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("version") == STATE_VERSION:
            first = data.get("first_missing_at")
            if isinstance(first, dict):
                return {k: v for k, v in first.items() if isinstance(k, str) and isinstance(v, (int, float))}
    except (OSError, ValueError):
        pass
    return {}


def save_state(path: str, first_missing: dict) -> None:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": STATE_VERSION, "first_missing_at": first_missing}, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def classify(
    row: dict,
    hub_info: dict | None,
    first_missing_at: float | None,
    now: float,
    grace_s: float,
) -> str:
    """Return ok|waiting|stop|report."""
    if not native_missing(row):
        return "ok"
    if is_interactive(row, hub_info):
        return "report"
    if first_missing_at is None:
        return "waiting"
    if (now - first_missing_at) >= grace_s:
        return "stop"
    return "waiting"


def run_cmd(argv: list[str], timeout: int = 30) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed: {argv[0]}")
    return proc.stdout


def sweep_once(
    *,
    sessions: list[dict],
    hub_lookup: Callable[[str], dict | None] | None = None,
    now: float | None = None,
    grace_hours: float = DEFAULT_GRACE_HOURS,
    state_path: str | None = None,
    stop_fn: Callable[[dict], bool] | None = None,
    observe_fn: Callable[[dict, dict | None], bool] | None = None,
    out: Callable[[str], None] | None = None,
    apply: bool = False,
) -> dict:
    """One bounded orphan sweep. Dry-run by default; apply=True permits stops."""
    emit = out or (lambda m: print(m, flush=True))
    now_v = now if isinstance(now, (int, float)) else time.time()
    grace_s = max(0.0, float(grace_hours) * 3600.0)
    first_missing = load_state(state_path) if state_path else {}
    counts = {"ok": 0, "waiting": 0, "stop": 0, "report": 0, "stopped": 0, "failed": 0, "would_stop": 0}
    live_ids: set[str] = set()
    dirty = False
    for row in sessions[:500]:
        sid = row.get("id")
        if not isinstance(sid, str) or not SESSION_ID_RE.match(sid):
            continue
        live_ids.add(sid)
        missing = native_missing(row)
        first = first_missing.get(sid)
        if not missing:
            if sid in first_missing:
                del first_missing[sid]
                dirty = True
            counts["ok"] += 1
            continue
        if not isinstance(first, (int, float)):
            first_missing[sid] = now_v
            dirty = True
            first = now_v
        hub_info = None
        if hub_lookup:
            try:
                hub_info = hub_lookup(sid)
            except Exception:
                hub_info = None
        action = classify(row, hub_info, float(first), now_v, grace_s)
        if action == "report":
            counts["report"] += 1
            emit(f"orphan session={sid} tag={row.get('tag')} action=report-only reason=worktree_missing interactive=true")
            if observe_fn:
                try:
                    observe_fn(row, hub_info)
                except Exception:
                    pass
            continue
        if action == "waiting":
            counts["waiting"] += 1
            emit(f"orphan session={sid} tag={row.get('tag')} action=wait reason=worktree_missing grace_h={grace_hours}")
            continue
        counts["stop"] += 1
        if not apply:
            counts["would_stop"] += 1
            emit(f"orphan session={sid} tag={row.get('tag')} action=would-stop reason=worktree_missing dry_run=true")
            if observe_fn:
                try:
                    observe_fn(row, hub_info)
                except Exception:
                    pass
            continue
        emit(f"orphan session={sid} tag={row.get('tag')} action=stop reason=worktree_missing")
        ok = False
        if stop_fn:
            try:
                ok = bool(stop_fn(row))
            except Exception:
                ok = False
        else:
            # Explicit apply=True required; library never kills by default.
            try:
                run_cmd(["aplexer", "kill", sid, "--grace-ms", "10000"])
                ok = True
            except Exception:
                ok = False
        if observe_fn:
            try:
                observe_fn(row, hub_info)
            except Exception:
                pass
        if ok:
            counts["stopped"] += 1
            emit(f"orphan session={sid} action=stopped reason=worktree_missing")
        else:
            counts["failed"] += 1
            emit(f"orphan session={sid} action=stop-failed reason=worktree_missing")
    # Drop journal entries for sessions no longer listed (bounded).
    for sid in list(first_missing.keys()):
        if sid not in live_ids:
            del first_missing[sid]
            dirty = True
    if dirty and state_path:
        save_state(state_path, first_missing)
    return counts


def default_hub_lookup(hub_bin: str = "hub", machine_id: str | None = None):
    def _lookup(sid: str) -> dict | None:
        try:
            raw = run_cmd([hub_bin, "sessions", "show", sid])
            info = json.loads(raw)
            if not isinstance(info, dict):
                return None
            return info
        except Exception:
            return None

    return _lookup


def default_observe(row: dict, hub_info: dict | None, hub_bin: str = "hub", machine_id: str | None = None) -> bool:
    if not machine_id:
        return False
    sid = str(row.get("id"))
    try:
        subprocess.run(
            [hub_bin, "sessions", "observe", "--machine-id", machine_id, "--native-id", sid, "--worktree-missing"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        return True
    except Exception:
        return False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--grace-hours", type=float, default=float(os.environ.get("ORPHAN_GRACE_HOURS", DEFAULT_GRACE_HOURS)))
    p.add_argument("--state-path", default=os.environ.get("ORPHAN_STATE_PATH", ".local/orphan-sweeper.json"))
    p.add_argument("--machine-id", default=os.environ.get("HUB_MACHINE_ID"))
    p.add_argument("--hub-bin", default="hub")
    p.add_argument("--apply", action="store_true", help="Actually stop expired orphans (default is dry-run report).")
    p.add_argument("--sessions-json", default=None, help="Prepared aplexer list JSON file (or - for stdin). Omit for live aplexer list.")
    return p


def main(argv: list[str] | None = None) -> dict:
    args = build_parser().parse_args(argv)
    if args.sessions_json in (None, ""):
        raw = run_cmd(["aplexer", "list", "--json"])
        try:
            sessions = json.loads(raw)
        except ValueError:
            raise SystemExit(2)
        if isinstance(sessions, dict):
            sessions = sessions.get("sessions", [])
    elif args.sessions_json == "-":
        import sys

        sessions = json.load(sys.stdin)
    else:
        with open(args.sessions_json, "r", encoding="utf-8") as fh:
            sessions = json.load(fh)
    if not isinstance(sessions, list):
        raise SystemExit(2)

    def _stop(row: dict) -> bool:
        if not args.apply:
            return False
        try:
            run_cmd(["aplexer", "kill", str(row.get("id")), "--grace-ms", "10000"])
            return True
        except Exception:
            return False

    def _observe(row: dict, hub_info: dict | None) -> bool:
        if not args.machine_id:
            return False
        return default_observe(row, hub_info, hub_bin=args.hub_bin, machine_id=args.machine_id)

    if args.apply and not args.machine_id:
        raise SystemExit("--apply requires --machine-id so stops are recorded on Hub via observe")
    result = sweep_once(
        sessions=sessions,
        hub_lookup=default_hub_lookup(hub_bin=args.hub_bin),
        grace_hours=args.grace_hours,
        state_path=args.state_path,
        stop_fn=_stop,
        observe_fn=_observe if args.machine_id else None,
        apply=args.apply,
    )
    print(json.dumps(result), flush=True)
    return result


if __name__ == "__main__":
    main()
