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
from typing import Any, Callable, Optional

DEFAULT_GRACE_HOURS = 2.0
STATE_VERSION = 2

SESSION_ID_RE = re.compile(r"[0-9a-fA-F-]{8,64}\Z")
TAG_HEADLESS_RE = re.compile(r"^(hub-|task-hub-)")

LIVE_PHASES = {"running", "starting", "active", "waiting"}


def _strip_deleted_suffix(path: str) -> str:
    if isinstance(path, str) and path.endswith(" (deleted)") and not os.path.exists(path):
        return path[: -len(" (deleted)")]
    return path


def path_recreated(candidate: Any, git_bin: str = "git", runner: Any = None, *, expected=None) -> bool:
    """True when an absent/deleted-labeled path now holds a valid worktree (AC5).

    The kernel keeps the `` (deleted)`` label on a process cwd even after the
    same path is recreated. Only absent or deleted-labeled candidates are
    examined; a present ordinary path is not "recreated".
    """
    if not isinstance(candidate, str) or not candidate:
        return False
    labeled = candidate.endswith(" (deleted)")
    if not labeled and os.path.exists(candidate):
        return False
    probe = candidate if labeled else candidate + " (deleted)"
    if os.path.exists(probe):
        # A literal same-named path exists: the label is real, not stale.
        return False
    try:
        from . import worktree_guard as _guard
    except Exception:
        return False
    try:
        return bool(_guard.detect_recreated(probe, git_bin, runner, expected=expected))
    except Exception:
        return False


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


def proc_cwd_target(pid: Any, readlink: Callable[[str], str] | None = None) -> str | None:
    """Retain the kernel target so stale inodes can be checked against repo evidence."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        target = (readlink or os.readlink)(f"/proc/{pid}/cwd")
        return target if isinstance(target, str) and os.path.isabs(target) else None
    except Exception:
        return None


def proc_cwd_deleted(pid: Any, readlink: Callable[[str], str] | None = None) -> bool | None:
    target = proc_cwd_target(pid, readlink)
    return target.endswith(" (deleted)") if target is not None else None


def cwd_candidates(row: dict) -> list:
    return [row.get("workspace"), row.get("cwd"),
            proc_cwd_target(row.get("workload_pid")), proc_cwd_target(row.get("worker_pid"))]


def native_missing(row: dict, recreated_checker: Any = None) -> bool:
    """True when workspace/cwd evidence proves the tree is gone. Unknown -> False.

    A path recreated as a valid worktree after its original inode was deleted
    (stale `` (deleted)`` kernel label) is NOT missing (AC5).
    """
    ws = row.get("workspace")
    cwd = row.get("cwd")
    if recreated_checker is None:
        from .worktree_guard import expected_repository
        expected = expected_repository(row)
        check = lambda candidate: path_recreated(candidate, expected=expected)
    else:
        check = recreated_checker
    candidates = cwd_candidates(row)
    for cand in candidates:
        if isinstance(cand, str) and cand and (cand.endswith(" (deleted)") or not os.path.exists(cand)):
            try:
                if check(cand):
                    return False
            except Exception:
                pass
    if isinstance(ws, str) and ws and workspace_missing(ws):
        return True
    if isinstance(cwd, str) and cwd.endswith(" (deleted)") and not os.path.exists(cwd):
        return True
    if any(isinstance(candidate, str) and candidate.endswith(" (deleted)")
           for candidate in candidates):
        return True
    return False


def is_live_row(row: dict, hub_info: dict | None) -> bool:
    """Best-effort liveness: native phase or Hub phase says running/starting."""
    for source in (row, hub_info):
        if not isinstance(source, dict):
            continue
        for key in ("phase", "state", "agent_state"):
            value = source.get(key)
            if isinstance(value, str) and value.strip().lower() in LIVE_PHASES:
                return True
    return False


def skip_reason(
    row: dict,
    hub_info: dict | None,
    *,
    locked_paths: Any = None,
    live_ids: Any = None,
    open_intake_sessions: Any = None,
) -> Optional[str]:
    """Worktree-guard skip (AC2): locked, live, or open-intake sessions are
    report-only, never stopped. Returns the reason or None."""
    sid = row.get("id") if isinstance(row, dict) else None
    if isinstance(sid, str):
        if isinstance(live_ids, (set, list, tuple)) and sid in live_ids:
            return "live session"
        if isinstance(open_intake_sessions, (set, list, tuple)) and sid in open_intake_sessions:
            return "linked open intake"
        if is_live_row(row if isinstance(row, dict) else {}, hub_info):
            return "live session"
    if isinstance(locked_paths, (set, list, tuple)) and locked_paths:
        normed = set()
        for raw in locked_paths:
            if isinstance(raw, str) and raw:
                normed.add(os.path.normpath(raw[: -len(" (deleted)")] if raw.endswith(" (deleted)") else raw))
        for key in ("workspace", "cwd"):
            cand = row.get(key) if isinstance(row, dict) else None
            if isinstance(cand, str) and cand:
                stripped = cand[: -len(" (deleted)")] if cand.endswith(" (deleted)") else cand
                if os.path.normpath(stripped) in normed:
                    return "worktree locked"
    return None


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


def _clean_first(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, (int, float))}


def load_state(path: str) -> dict:
    """Return {sid: first_missing_at}. Reads v1 and v2 state files."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("version") in (1, STATE_VERSION):
            return _clean_first(data.get("first_missing_at"))
    except (OSError, ValueError):
        pass
    return {}


def load_alerted(path: str) -> dict:
    """Return {sid: alerted_at} from a v2 state file ({} for v1/absent)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("version") == STATE_VERSION:
            return _clean_first(data.get("alerted_at"))
    except (OSError, ValueError):
        pass
    return {}


def save_state(path: str, first_missing: dict) -> None:
    """Persist first-missing map, preserving any v2 alerted_at entries."""
    alerted: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("version") == STATE_VERSION:
            alerted = _clean_first(data.get("alerted_at"))
    except (OSError, ValueError):
        pass
    save_full_state(path, first_missing, alerted)


def save_full_state(path: str, first_missing: dict, alerted_at: dict) -> None:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": STATE_VERSION, "first_missing_at": first_missing,
                       "alerted_at": alerted_at}, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def classify(
    row: dict,
    hub_info: dict | None,
    first_missing_at: float | None,
    now: float,
    grace_s: float,
    recreated_checker: Any = None,
) -> str:
    """Return ok|waiting|stop|report."""
    if not native_missing(row, recreated_checker):
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
    locked_paths: Any = None,
    live_ids: Any = None,
    open_intake_sessions: Any = None,
    cleanup_allowed: Callable[[dict], bool] | None = None,
    alert_fn: Callable[[dict, dict | None], bool] | None = None,
    observe_recreated_fn: Callable[[dict, dict | None], bool] | None = None,
    observe_recovered_fn: Callable[[dict, dict | None], bool] | None = None,
    recreated_checker: Any = None,
) -> dict:
    """One bounded orphan sweep. Dry-run by default; apply=True permits stops.

    Worktree-guard (intake a0f01448):
    - recreated worktree paths (stale `` (deleted)`` label, AC5) count as
      ``recreated``, never as missing;
    - locked / live / open-intake sessions (AC2) are report-only, never
      stopped or would-stopped;
    - the first detection of a missing tree immediately alerts (AC4) via
      ``alert_fn`` (default: ``observe_fn``) so Hub opens a needs_attention
      item and appends the linked-intake note within one sweeper interval;
      the stop itself stays grace-gated.
    """
    emit = out or (lambda m: print(m, flush=True))
    now_v = now if isinstance(now, (int, float)) else time.time()
    grace_s = max(0.0, float(grace_hours) * 3600.0)
    first_missing = load_state(state_path) if state_path else {}
    alerted_at = load_alerted(state_path) if state_path else {}
    counts = {"ok": 0, "waiting": 0, "stop": 0, "report": 0, "stopped": 0,
              "failed": 0, "would_stop": 0, "recreated": 0, "alerted": 0}
    seen_ids: set[str] = set()
    dirty = False
    alert_dirty = False
    for row in sessions:
        sid = row.get("id")
        if not isinstance(sid, str) or not SESSION_ID_RE.match(sid):
            continue
        seen_ids.add(sid)
        if recreated_checker is None:
            from .worktree_guard import expected_repository
            expected = expected_repository(row)
            check = lambda candidate: path_recreated(candidate, expected=expected)
        else:
            check = recreated_checker

        def _candidate_recreated(cand: Any) -> bool:
            if not isinstance(cand, str) or not cand:
                return False
            if not (cand.endswith(" (deleted)") or not os.path.exists(cand)):
                return False
            try:
                return bool(check(cand))
            except Exception:
                return False

        recreated = any(_candidate_recreated(candidate) for candidate in cwd_candidates(row))
        if recreated:
            counts["recreated"] += 1
            emit(f"orphan session={sid} tag={row.get('tag')} action=ok reason=cwd_stale_path_recreated")
            delivered = sid not in alerted_at
            if observe_recreated_fn:
                try:
                    delivered = observe_recreated_fn(row, None) is not False
                except Exception:
                    delivered = False
            if delivered:
                if sid in first_missing:
                    del first_missing[sid]
                    dirty = True
                if sid in alerted_at:
                    del alerted_at[sid]
                    alert_dirty = True
            continue
        missing = native_missing(row, recreated_checker)
        first = first_missing.get(sid)
        if not missing:
            if sid in alerted_at:
                try:
                    if observe_recovered_fn:
                        if observe_recovered_fn(row, None) is False:
                            continue
                except Exception:
                    continue  # Retry recovery reporting next cycle.
                del alerted_at[sid]
                alert_dirty = True
            if sid in first_missing:
                del first_missing[sid]
                dirty = True
            counts["ok"] += 1
            continue
        hub_info = None
        if hub_lookup:
            try:
                hub_info = hub_lookup(sid)
            except Exception:
                hub_info = None
        if not isinstance(first, (int, float)):
            first_missing[sid] = now_v
            dirty = True
            first = now_v
        # AC4: alert Hub on first detection, without waiting for grace.
        # The installed observer repeats the episode observation so a late
        # binding/intake link receives its note. Server episode IDs deduplicate
        # durable effects; an ACK never suppresses future linkage discovery.
        if sid not in alerted_at or alert_fn is not None:
            alerter = alert_fn or observe_fn
            if alerter is not None:
                try:
                    delivered = alerter(row, hub_info)
                except Exception:
                    pass
                else:
                    if delivered is not False:
                        first_ack = sid not in alerted_at
                        alerted_at[sid] = now_v
                        alert_dirty = True
                        counts["alerted"] += int(first_ack)
        reason = skip_reason(row, hub_info, locked_paths=locked_paths,
                             live_ids=live_ids, open_intake_sessions=open_intake_sessions)
        if reason is not None:
            counts["report"] += 1
            if sid in first_missing:
                del first_missing[sid]
                dirty = True
            emit(f"orphan session={sid} tag={row.get('tag')} action=report-only reason=worktree_missing guard_skip={reason}")
            if observe_fn:
                try:
                    observe_fn(row, hub_info)
                except Exception:
                    pass
            continue
        if apply:
            try:
                allowed = cleanup_allowed is not None and cleanup_allowed(row) is True
            except Exception:
                allowed = False
            if not allowed:
                counts["report"] += 1
                emit(f"orphan session={sid} action=report-only reason=cleanup_authority_unproven")
                continue
        action = classify(row, hub_info, float(first), now_v, grace_s, recreated_checker)
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
        if sid not in seen_ids:
            del first_missing[sid]
            dirty = True
    for sid in list(alerted_at.keys()):
        if sid not in seen_ids:
            del alerted_at[sid]
            alert_dirty = True
    if (dirty or alert_dirty) and state_path:
        save_full_state(state_path, first_missing, alerted_at)
    return counts


def observe_catalog(sessions, request, machine_id: str, journal_dir: str):
    """Report-only observer used by the installed immutable host every minute.

    Uses host-authenticated HTTP directly. No subprocess CLI/config lookup and
    no stop callback: interactive and protected sessions stay report-only.
    """
    def observe(row, info):
        result = request("POST", "/api/sessions/observations", {
            "machine_id": machine_id, "native_id": row["id"],
            "payload": {"worktree_missing": True, "cwd_path_recreated": False}})
        return isinstance(result, dict) and result.get("worktree_followup_complete") is True

    def recovered(row, info):
        result = request("POST", "/api/sessions/observations", {
            "machine_id": machine_id, "native_id": row["id"],
            "payload": {"worktree_missing": False, "cwd_path_recreated": False}})
        return isinstance(result, dict) and result.get("worktree_followup_complete") is True

    def recreated(row, info):
        result = request("POST", "/api/sessions/observations", {
            "machine_id": machine_id, "native_id": row["id"],
            "payload": {"worktree_missing": False, "cwd_path_recreated": True}})
        return isinstance(result, dict) and result.get("worktree_followup_complete") is True

    return sweep_once(sessions=sessions, apply=False, observe_fn=None,
                      alert_fn=observe, observe_recreated_fn=recreated, observe_recovered_fn=recovered,
                      state_path=os.path.join(journal_dir, "worktree-observations.json"),
                      out=lambda message: None)


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
        result = subprocess.run(
            [hub_bin, "sessions", "observe", "--machine-id", machine_id, "--native-id", sid, "--worktree-missing"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def default_observe_recreated(row: dict, hub_info: dict | None, hub_bin: str = "hub", machine_id: str | None = None) -> bool:
    """Report a recreated worktree path so Hub shows the distinct stale-path state."""
    if not machine_id:
        return False
    sid = str(row.get("id"))
    try:
        result = subprocess.run(
            [hub_bin, "sessions", "observe", "--machine-id", machine_id, "--native-id", sid, "--cwd-path-recreated"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        return result.returncode == 0
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

    def _observe_recreated(row: dict, hub_info: dict | None) -> bool:
        if not args.machine_id:
            return False
        return default_observe_recreated(row, hub_info, hub_bin=args.hub_bin, machine_id=args.machine_id)

    if args.apply and not args.machine_id:
        raise SystemExit("--apply requires --machine-id so stops are recorded on Hub via observe")
    from .worktree_guard import cleanup_permitted
    from .provisioning import host_request
    result = sweep_once(
        cleanup_allowed=lambda row: cleanup_permitted(row, host_request),
        sessions=sessions,
        hub_lookup=default_hub_lookup(hub_bin=args.hub_bin),
        grace_hours=args.grace_hours,
        state_path=args.state_path,
        stop_fn=_stop,
        observe_fn=_observe if args.machine_id else None,
        observe_recreated_fn=_observe_recreated if args.machine_id else None,
        apply=args.apply,
    )
    print(json.dumps(result), flush=True)
    return result


if __name__ == "__main__":
    main()
