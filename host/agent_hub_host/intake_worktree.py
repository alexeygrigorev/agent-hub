"""Intake worktrees, host side (intake 4b161918; contract docs/INTAKE-WORKTREES.md).

The host agent owns three effects for Hub-recorded intake worktrees:

- status: report ``{present, dirty, dirty_count, unpushed, unmerged, merged,
  head, branch, checked_at, error}``. Unknown values are ``None``, never
  invented. ``unmerged``/``merged`` are only computed after a fresh
  ``git fetch origin main``.
- remove: guarded ``git worktree remove <path>`` (never ``--force``, never
  rm), ``git worktree prune``, then ``git branch -d`` only when merged
  (never ``-D``). Every refusal names its reason.
- ensure (launch): create, reuse or recreate the dedicated worktree a Hub
  launch plan names, from fresh ``origin/main``.

Nothing here deletes automatically. Stdlib only; every git call is an argv
array run without a shell.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .native_sessions import SessionControlError
from .session_cleanup import BRANCH, _git_common_dir
from .worktree_guard import _parse_worktree_list, parse_lock_reason

GIT_TIMEOUT = 30.0
FETCH_TIMEOUT = 60.0
OUTPUT_LIMIT = 1024 * 1024
MAX_COMMANDS = 10
COMMAND_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
SUFFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
STATUS_KEYS = ("present", "dirty", "dirty_count", "unpushed", "unmerged", "merged",
               "head", "branch", "checked_at", "error")
PLAN_PATH = "/api/worktree-host/plan"
OBSERVE_PATH = "/api/worktree-host/observe"
COMMANDS_PATH = "/api/worktree-host/commands"


class WorktreeGuardError(SessionControlError, ValueError):
    """The path is not an allowed intake worktree path; nothing was done."""


# --------------------------------------------------------------------------
# helpers

def _now_ms() -> int:
    return int(time.time() * 1000)


def _short(text: Any, limit: int = 200) -> str:
    line = ""
    for candidate in str(text or "").splitlines():
        if candidate.strip():
            line = candidate.strip()
            break
    return line[:limit]


def default_runner(argv: list[str], cwd: str, timeout: float = GIT_TIMEOUT):
    """Run one git argv (no shell), bounded in time and output, never prompting."""
    if not isinstance(argv, list) or any(not isinstance(a, str) or not a for a in argv):
        raise SessionControlError("git argv must be a non-empty string list")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true", LC_ALL="C")
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          check=False, cwd=cwd, env=env, stdin=subprocess.DEVNULL)
    for stream in (proc.stdout, proc.stderr):
        if stream and len(stream.encode("utf-8", "replace")) > OUTPUT_LIMIT:
            raise SessionControlError("git output exceeds bound")
    return proc


def _git(runner: Callable, git_bin: str, args: list[str], cwd: str,
         timeout: float = GIT_TIMEOUT):
    """Run git; returns (proc or None, error string or None). Never raises."""
    try:
        proc = runner([git_bin, *args], cwd, timeout)
    except subprocess.TimeoutExpired:
        return None, f"git {args[0]} timed out"
    except Exception as exc:  # noqa: BLE001 - every git failure is reported, not raised
        return None, f"git {args[0]} failed: {type(exc).__name__}: {_short(exc)}"
    if proc.returncode != 0:
        return proc, f"git {args[0]} failed: {_short(proc.stderr) or 'exit ' + str(proc.returncode)}"
    return proc, None


def _home(home: Optional[str]) -> str:
    return os.path.abspath(home if home is not None else os.path.expanduser("~"))


def path_guard(path: Any, home: Optional[str] = None) -> str:
    """Return the canonical path when it is an allowed intake worktree path.

    Rules (contract "Path guard"): absolute and normalized (no ``..``), the
    realpath equals the path (no symlinked components), the parent directory
    is exactly ``<home>/git``, the basename contains ``-wt-`` and the path
    never contains ``/prod``. Raises :class:`WorktreeGuardError` otherwise.
    """
    if not isinstance(path, str) or not path or "\x00" in path:
        raise WorktreeGuardError("worktree path is required")
    if "/prod" in path:
        raise WorktreeGuardError("worktree path contains /prod")
    if not os.path.isabs(path):
        raise WorktreeGuardError("worktree path must be absolute")
    absolute = os.path.abspath(path)
    if absolute != path.rstrip("/") or ".." in path.split("/"):
        raise WorktreeGuardError("worktree path must be normalized")
    if os.path.realpath(absolute) != absolute:
        raise WorktreeGuardError("worktree path resolves through a symlink")
    git_root = os.path.join(_home(home), "git")
    if os.path.dirname(absolute) != git_root:
        raise WorktreeGuardError(f"worktree path must sit directly under {git_root}")
    if "-wt-" not in os.path.basename(absolute):
        raise WorktreeGuardError("worktree basename must contain -wt-")
    return absolute


def repo_hint_for(path: str) -> Optional[str]:
    """Main checkout guess for a worktree path: dirname/<basename up to -wt->."""
    base = os.path.basename(path)
    if "-wt-" not in base:
        return None
    prefix = base.split("-wt-", 1)[0]
    return os.path.join(os.path.dirname(path), prefix) if prefix else None


def _registry_entry(git_bin: str, path: str, runner: Callable, cwd: Optional[str] = None):
    """Return (entry or None, entries, error) for `path` in the worktree registry."""
    proc, err = _git(runner, git_bin, ["worktree", "list", "--porcelain"], cwd or path)
    if err:
        return None, [], err
    try:
        entries = _parse_worktree_list(proc.stdout or "")
    except Exception as exc:  # noqa: BLE001
        return None, [], _short(exc)
    want = os.path.normpath(path)
    for entry in entries:
        if os.path.normpath(entry["path"]) == want:
            return entry, entries, None
    return None, entries, None


def _is_repo(git_bin: str, path: Optional[str], runner: Callable) -> bool:
    """True only when `path` is itself a checkout's top level.

    A directory nested inside some other repository (git discovery walks up)
    is never treated as the workspace repository.
    """
    if not path or not os.path.isdir(path):
        return False
    proc, err = _git(runner, git_bin, ["rev-parse", "--show-toplevel"], path)
    if err is not None:
        return False
    top = (proc.stdout or "").strip()
    return bool(top) and os.path.realpath(top) == os.path.realpath(path)


def _merge_state(git_bin: str, cwd: str, rev: str, runner: Callable, status: dict) -> None:
    """Fill unmerged/merged for `rev` after a fresh fetch; unknown stays None."""
    _, err = _git(runner, git_bin, ["fetch", "--quiet", "origin", "main"], cwd, FETCH_TIMEOUT)
    if err:
        status["error"] = err
        return
    cherry, cherry_err = _git(runner, git_bin, ["cherry", "origin/main", rev], cwd)
    if cherry_err is None:
        status["unmerged"] = sum(1 for line in (cherry.stdout or "").splitlines() if line.startswith("+"))
    ancestor, _ = _git(runner, git_bin, ["merge-base", "--is-ancestor", rev, "origin/main"], cwd)
    if ancestor is not None and ancestor.returncode == 0:
        status["merged"] = True
    elif status["unmerged"] == 0:
        status["merged"] = True
    elif status["unmerged"] is not None and ancestor is not None and ancestor.returncode == 1:
        status["merged"] = False
    if status["merged"] is None:
        status["error"] = cherry_err or "merge state unknown"


def _inspect(path: str, *, git_bin: str, runner: Callable, branch: Optional[str],
             repo_hint: Optional[str], fetch: bool = True):
    """Status plus the registry entry (for lock/detached checks)."""
    status: dict[str, Any] = {key: None for key in STATUS_KEYS}
    status["checked_at"] = _now_ms()
    entry = None
    if not os.path.isdir(path):
        status["present"] = False
        status["branch"] = branch
        hint = repo_hint if repo_hint is not None else repo_hint_for(path)
        if branch and isinstance(branch, str) and BRANCH.fullmatch(branch) and _is_repo(git_bin, hint, runner):
            ref = "refs/heads/" + branch
            proc, err = _git(runner, git_bin, ["rev-parse", "--verify", "--quiet", ref + "^{commit}"], hint)
            if err:
                status["error"] = f"branch {branch} not found in {hint}"
            else:
                status["head"] = (proc.stdout or "").strip() or None
                if fetch:
                    _merge_state(git_bin, hint, ref, runner, status)
        elif branch:
            status["error"] = "main repository unknown"
        return status, entry
    entry, _, err = _registry_entry(git_bin, path, runner)
    if err or entry is None:
        status["present"] = False
        status["error"] = "not a registered git worktree" + (f" ({err})" if err else "")
        return status, None
    status["present"] = True
    status["head"] = entry.get("head")
    ref = entry.get("branch")
    status["branch"] = ref[len("refs/heads/"):] if isinstance(ref, str) and ref.startswith("refs/heads/") else None
    porcelain, err = _git(runner, git_bin, ["status", "--porcelain=v1", "--untracked-files=all"], path)
    if err is None:
        count = sum(1 for line in (porcelain.stdout or "").splitlines() if line.strip())
        status["dirty_count"] = count
        status["dirty"] = count > 0
    else:
        status["error"] = err
    unpushed, err = _git(runner, git_bin, ["rev-list", "--count", "HEAD", "--not", "--remotes"], path)
    if err is None:
        try:
            status["unpushed"] = int((unpushed.stdout or "").strip())
        except ValueError:
            pass
    if fetch:
        before = status["error"]
        _merge_state(git_bin, path, "HEAD", runner, status)
        if before and status["error"] != before:
            status["error"] = before + "; " + status["error"]
    return status, entry


# --------------------------------------------------------------------------
# public API

def worktree_status(path: str, git_bin: str = "git", runner: Optional[Callable] = None, *,
                    branch: Optional[str] = None, repo_hint: Optional[str] = None,
                    fetch: bool = True) -> dict[str, Any]:
    """Host status of one worktree (contract `status` shape)."""
    status, _ = _inspect(path, git_bin=git_bin, runner=runner or default_runner,
                         branch=branch, repo_hint=repo_hint, fetch=fetch)
    return status


def live_processes_in(path: str, proc_root: str = "/proc") -> list[int]:
    """PIDs (other than ours) whose cwd is inside `path`.

    Raises SessionControlError when /proc is unavailable. Processes whose cwd
    cannot be read (exited, other users) are skipped.
    """
    if not os.path.isdir(proc_root):
        raise SessionControlError("cannot verify live processes")
    try:
        names = os.listdir(proc_root)
    except OSError as exc:
        raise SessionControlError("cannot verify live processes") from exc
    own = os.getpid()
    prefix = path.rstrip("/") + "/"
    found = []
    for name in names:
        if not name.isdigit() or int(name) == own:
            continue
        try:
            cwd = os.readlink(os.path.join(proc_root, name, "cwd"))
        except OSError:
            continue
        if cwd.endswith(" (deleted)"):
            cwd = cwd[: -len(" (deleted)")]
        if cwd == path or cwd.startswith(prefix):
            found.append(int(name))
    return sorted(found)


def _result(outcome: str, reason: Optional[str], status: Optional[dict], removed=False,
            branch_deleted=False) -> dict[str, Any]:
    return {"outcome": outcome, "reason": reason, "removed": removed,
            "branch_deleted": branch_deleted, "status": status}


def remove_worktree(path: str, branch: Optional[str] = None, *, live_check: Optional[Callable] = None,
                    git_bin: str = "git", runner: Optional[Callable] = None,
                    home: Optional[str] = None, fetch: bool = True,
                    task_id: Optional[str] = None, unlock_hub_lock: bool = False) -> dict[str, Any]:
    """Guarded removal: {outcome: ok|refused|error, reason, removed, branch_deleted, status}."""
    runner = runner or default_runner
    try:
        path = path_guard(path, home)
    except WorktreeGuardError as exc:
        return _result("refused", f"path guard: {exc}", None)
    status, entry = _inspect(path, git_bin=git_bin, runner=runner, branch=branch,
                             repo_hint=None, fetch=fetch)
    if not status["present"] or entry is None:
        reason = "not present" if not os.path.isdir(path) else (status["error"] or "not a registered git worktree")
        return _result("refused", reason, status)
    if entry.get("bare"):
        return _result("refused", "bare repository", status)
    parsed_lock = parse_lock_reason(entry.get("reason")) if entry.get("locked") else None
    can_unlock_hub = bool(
        unlock_hub_lock
        and parsed_lock is not None
        and (parsed_lock[1] is None or parsed_lock[1] == task_id)
    )
    if entry.get("locked") and not can_unlock_hub:
        return _result("refused", "worktree locked", status)
    if entry.get("detached") or not status["branch"]:
        return _result("refused", "detached HEAD", status)
    if branch and branch != status["branch"]:
        return _result("refused", f"branch mismatch: worktree is on {status['branch']}", status)
    if status["dirty"] is None:
        return _result("refused", f"dirty state unknown: {status['error']}", status)
    if status["dirty"]:
        return _result("refused", f"dirty: {status['dirty_count']} uncommitted changes", status)
    if status["merged"] is None:
        return _result("refused", f"merge state unknown: {status['error']}", status)
    if not status["merged"]:
        unmerged = status["unmerged"]
        if status["unpushed"]:
            return _result("refused", f"unpushed commits {status['unpushed']}", status)
        return _result("refused", f"unmerged commits {unmerged}", status)
    try:
        pids = (live_check or live_processes_in)(path)
    except Exception as exc:  # noqa: BLE001
        return _result("refused", _short(exc) or "cannot verify live processes", status)
    if pids:
        return _result("refused", f"live process has cwd inside worktree (pid {pids[0]})", status)
    try:
        common = _git_common_dir(git_bin, path, runner)
    except Exception as exc:  # noqa: BLE001
        return _result("error", f"main repository unknown: {_short(exc)}", status)
    main = os.path.dirname(common) if os.path.basename(common) == ".git" else common
    if main == path or main.startswith(path + "/") or not os.path.isdir(main):
        return _result("error", "main repository unknown", status)
    if entry.get("locked") and can_unlock_hub:
        _, unlock_err = _git(runner, git_bin, ["worktree", "unlock", path], main)
        if unlock_err:
            return _result("error", unlock_err, status)
    _, err = _git(runner, git_bin, ["worktree", "remove", path], main)
    if err:
        if entry.get("locked") and can_unlock_hub and entry.get("reason"):
            _git(runner, git_bin, ["worktree", "lock", "--reason", str(entry["reason"]), path], main)
        return _result("error", err, status)
    removed = not os.path.lexists(path)
    _, prune_err = _git(runner, git_bin, ["worktree", "prune"], main)
    final = dict(status, present=False if removed else status["present"], checked_at=_now_ms())
    if not removed:
        return _result("error", "git worktree remove reported success but the path still exists", final)
    reason = None if prune_err is None else f"removed; {prune_err}"
    branch_deleted = False
    _, branch_err = _git(runner, git_bin, ["branch", "-d", status["branch"]], main)
    if branch_err is None:
        branch_deleted = True
    else:
        detail = branch_err.split(": ", 1)[-1]
        reason = (reason + "; " if reason else "removed; ") + "branch -d failed: " + detail
    return _result("ok", reason, final, removed=True, branch_deleted=branch_deleted)


def ensure_intake_worktree(workspace: Any, plan: Any, git_bin: str = "git",
                           runner: Optional[Callable] = None, *,
                           home: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Apply a Hub launch plan: {action: created|reused|recreated|failed, path, branch, error}.

    Returns None for ``{mode: "none"}`` (or no plan). Never deletes anything;
    on failure only git's own cleanup of a failed ``worktree add`` happens.
    """
    runner = runner or default_runner
    if not isinstance(plan, dict) or plan.get("mode") in (None, "none"):
        return None
    mode = plan.get("mode")
    ws = os.path.abspath(str(workspace)) if workspace else ""
    out: dict[str, Any] = {"action": "failed", "path": None, "branch": None, "error": None}

    def fail(error: str) -> dict[str, Any]:
        out["error"] = error
        return out

    if mode == "resume":
        path, branch = plan.get("path"), plan.get("branch")
        if not isinstance(path, str):
            return fail("plan path missing")
    elif mode == "create":
        suffix = plan.get("suffix")
        if not isinstance(suffix, str) or not SUFFIX.fullmatch(suffix) or not ws:
            return fail("plan suffix invalid")
        name = os.path.basename(ws) + "-" + suffix
        path, branch = os.path.join(os.path.dirname(ws), name), name
    else:
        return fail(f"unsupported plan mode {mode!r}")
    out["path"], out["branch"] = path, branch if isinstance(branch, str) else None
    if branch is not None and (not isinstance(branch, str) or not BRANCH.fullmatch(branch)
                               or branch.startswith(("-", ".", "/")) or ".." in branch):
        return fail("invalid branch name")
    try:
        path = path_guard(path, home)
    except WorktreeGuardError as exc:
        return fail(f"path guard: {exc}")
    out["path"] = path
    if not os.path.isdir(os.path.dirname(path)):
        return fail("parent directory missing")
    if os.path.lexists(path):
        entry, _, err = _registry_entry(git_bin, path, runner) if os.path.isdir(path) else (None, [], None)
        if entry is None:
            return fail(err or "path exists and is not a registered git worktree")
        ref = entry.get("branch")
        if isinstance(ref, str) and ref.startswith("refs/heads/"):
            out["branch"] = ref[len("refs/heads/"):]
        out["action"] = "reused"
        return out
    if not branch:
        return fail("branch unknown; cannot recreate")
    # Recreate / create from the main repository: the workspace for create,
    # the workspace (or the path's main-checkout hint) for resume.
    repo = ws if _is_repo(git_bin, ws, runner) else None
    if repo is None and mode == "resume" and _is_repo(git_bin, repo_hint_for(path), runner):
        repo = repo_hint_for(path)
    if repo is None:
        return fail("workspace is not a git repository")
    _, err = _git(runner, git_bin, ["remote", "get-url", "origin"], repo)
    if err:
        return fail("no origin remote")
    _, err = _git(runner, git_bin, ["fetch", "--quiet", "origin", "main"], repo, FETCH_TIMEOUT)
    if err:
        return fail(err)
    _, err = _git(runner, git_bin, ["rev-parse", "--verify", "--quiet", "origin/main^{commit}"], repo)
    if err:
        return fail("origin/main unknown")
    stale, _, _ = _registry_entry(git_bin, path, runner, cwd=repo)
    if stale is not None:
        # Registered but the directory is gone: git's own metadata cleanup only.
        _git(runner, git_bin, ["worktree", "prune"], repo)
    exists, _ = _git(runner, git_bin, ["show-ref", "--verify", "--quiet", "refs/heads/" + branch], repo)
    if exists is not None and exists.returncode == 0:
        _, err = _git(runner, git_bin, ["worktree", "add", path, branch], repo)
    else:
        _, err = _git(runner, git_bin, ["worktree", "add", "-b", branch, path, "origin/main"], repo)
    if err:
        return fail(err)
    out["action"] = "recreated" if mode == "resume" else "created"
    return out


# --------------------------------------------------------------------------
# Hub plumbing

def is_not_found(exc: BaseException) -> bool:
    """True for the 404 an older Hub (no worktree-host routes) returns."""
    return bool(re.search(r"\bstatus 404\b", str(exc)))


def prompt_line(task_id: Any, path: str, branch: Optional[str]) -> str:
    task8 = str(task_id or "")[:8]
    return (f"Hub prepared the dedicated worktree for intake {task8}: {path} "
            f"(branch {branch}, from origin/main). Work and commit there; do not create another worktree.")


def prepare_launch_worktree(request: Callable, launch_id: str, workspace: Any, *,
                            git_bin: str = "git", runner: Optional[Callable] = None,
                            home: Optional[str] = None, make_tmp: bool = False,
                            log: Callable = print) -> Optional[dict[str, Any]]:
    """Fleet launch hook. Returns the ensured worktree (plus ``prompt_line``) or None.

    Fail-open: an old Hub (404), a network error or a git failure returns None
    so the launch proceeds in the workspace exactly as before. Outcomes are
    reported through ``/api/worktree-host/observe`` best effort.
    """
    def observe(body: dict) -> None:
        try:
            request("POST", OBSERVE_PATH, body)
        except Exception as exc:  # noqa: BLE001
            if not is_not_found(exc):
                log_event("intake_worktree_observe_error", type(exc).__name__)

    def log_event(event: str, detail: str) -> None:
        try:
            import json
            log(json.dumps({"event": event, "launch_id": launch_id, "detail": detail[:200]}))
        except Exception:  # noqa: BLE001
            pass

    try:
        plan = request("POST", PLAN_PATH, {"launch_id": launch_id})
    except Exception as exc:  # noqa: BLE001
        if is_not_found(exc):
            return None
        log_event("intake_worktree_plan_error", type(exc).__name__)
        observe({"launch_id": launch_id, "action": "failed", "path": None, "branch": None,
                 "error": f"plan request failed: {type(exc).__name__}"})
        return None
    if isinstance(plan, dict) and isinstance(plan.get("plan"), dict):
        plan = plan["plan"]  # Hub replies {"plan": {...}}
    if not isinstance(plan, dict):
        return None
    if plan.get("mode") == "none" and plan.get("read_only") is True:
        return {"action": "read_only", "path": None, "branch": None, "prompt_line": ""}
    if plan.get("mode") not in ("resume", "create"):
        return None
    try:
        result = ensure_intake_worktree(workspace, plan, git_bin, runner, home=home)
    except Exception as exc:  # noqa: BLE001
        result = {"action": "failed", "path": plan.get("path"), "branch": plan.get("branch"),
                  "error": f"{type(exc).__name__}: {_short(exc)}"}
    if result is None:
        return None
    if result["action"] != "failed" and make_tmp:
        try:
            os.makedirs(os.path.join(result["path"], ".local", "tmp"), mode=0o700, exist_ok=True)
        except OSError as exc:
            result = dict(result, action="failed", error=f"tmpdir: {type(exc).__name__}")
    body = {"launch_id": launch_id, "action": result["action"], "path": result["path"],
            "branch": result["branch"]}
    if result.get("error"):
        body["error"] = result["error"]
    observe(body)
    if result["action"] == "failed":
        log_event("intake_worktree_failed", str(result.get("error")))
        return None
    return dict(result, prompt_line=prompt_line(plan.get("task_id"), result["path"], result["branch"]))


def launch_cwd(request: Callable, offer: dict, workspace: Any, target_cwd: Any, *,
               make_tmp: bool = False):
    """Fleet dispatch hook: ``(target_cwd, prompt_note, worktree_or_None)``.

    Only offers without an explicit cwd ask Hub for a plan. Any failure keeps
    the caller's target_cwd and an empty note (fail-open).
    """
    explicit = offer.get("cwd") is not None
    try:
        # An explicit cwd is still reported so Hub records (and refuses sharing
        # of) a session-named worktree; Hub plans mode "none" for it.
        wt = prepare_launch_worktree(request, offer.get("id"), workspace, make_tmp=make_tmp)
    except Exception:  # noqa: BLE001 - never lose a launch to this feature
        wt = None
    if wt is None or explicit:
        return target_cwd, "", None
    if wt.get("action") == "read_only":
        # Intake ccc50cca: read-only review sessions must never run inside a
        # linked -wt- checkout (or any subdirectory of one) where _lock_session_worktree
        # would collide with the implementer's lock. Map back to the main checkout.
        raw_cwd = str(target_cwd) if target_cwd is not None else ""
        if raw_cwd and os.path.isdir(raw_cwd):
            try:
                proc = _default_runner(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], raw_cwd, GIT_TIMEOUT)
                if proc.returncode == 0:
                    common = Path(os.path.realpath(proc.stdout.strip()))
                    if common.name == ".git" and common.parent.is_dir():
                        return common.parent, "", None
            except Exception:  # noqa: BLE001
                pass
        parts = Path(raw_cwd).parts if raw_cwd else ()
        wt_idx = max((i for i, p in enumerate(parts) if "-wt-" in p), default=None)
        if wt_idx is not None:
            wt_root = Path(*parts[: wt_idx + 1])
            main_name = re.sub(r"-wt-[A-Za-z0-9._-]+$", "", wt_root.name)
            candidate = wt_root.parent / main_name
            if candidate.is_dir():
                return candidate, "", None
        return target_cwd, "", None
    return Path(wt["path"]), wt["prompt_line"] + "\n", wt


def _run_command(cmd: dict, *, git_bin: str, runner: Optional[Callable], home: Optional[str],
                 live_check: Optional[Callable]) -> dict[str, Any]:
    kind, path, branch = cmd.get("kind"), cmd.get("path"), cmd.get("branch")
    if not isinstance(branch, str) or not branch:
        branch = None
    if kind == "status":
        try:
            path = path_guard(path, home)
        except WorktreeGuardError as exc:
            return {"outcome": "refused", "reason": f"path guard: {exc}", "status": None,
                    "removed": False, "branch_deleted": False}
        status = worktree_status(path, git_bin, runner, branch=branch)
        return {"outcome": "ok", "reason": status.get("error"), "status": status,
                "removed": False, "branch_deleted": False}
    if kind == "remove":
        return remove_worktree(
            path, branch, live_check=live_check, git_bin=git_bin,
            runner=runner, home=home,
            task_id=cmd.get("task_id") if isinstance(cmd.get("task_id"), str) else None,
            unlock_hub_lock=cmd.get("unlock_hub_lock") is True,
        )
    return {"outcome": "error", "reason": f"unsupported command kind {kind!r}", "status": None,
            "removed": False, "branch_deleted": False}


def poll_commands_once(request: Callable, *, git_bin: str = "git", runner: Optional[Callable] = None,
                       home: Optional[str] = None, live_check: Optional[Callable] = None,
                       limit: int = MAX_COMMANDS, log: Callable = print) -> dict[str, Any]:
    """One bounded pass over the machine's worktree command queue.

    Returns ``{supported, handled, errors}``; ``supported`` is False when the
    Hub predates these routes (404). Other GET failures propagate to the
    caller's best-effort wrapper.
    """
    try:
        reply = request("GET", COMMANDS_PATH)
    except Exception as exc:  # noqa: BLE001
        if is_not_found(exc):
            return {"supported": False, "handled": 0, "errors": 0}
        raise
    commands = reply.get("commands") if isinstance(reply, dict) else None
    handled = errors = 0
    for cmd in (commands if isinstance(commands, list) else [])[:max(0, min(limit, MAX_COMMANDS))]:
        if not isinstance(cmd, dict) or not isinstance(cmd.get("id"), str) or not COMMAND_ID.fullmatch(cmd["id"]):
            errors += 1
            continue
        try:
            result = _run_command(cmd, git_bin=git_bin, runner=runner, home=home, live_check=live_check)
        except Exception as exc:  # noqa: BLE001
            result = {"outcome": "error", "reason": f"{type(exc).__name__}: {_short(exc)}"[:200],
                      "status": None, "removed": False, "branch_deleted": False}
        body = {k: result.get(k) for k in ("outcome", "reason", "status", "removed", "branch_deleted")}
        if body["reason"] is None:
            body.pop("reason")
        if body["status"] is None:
            body.pop("status")
        try:
            request("POST", f"{COMMANDS_PATH}/{cmd['id']}/result", body)
            handled += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            try:
                import json
                log(json.dumps({"event": "intake_worktree_result_error", "command": cmd["id"],
                                "type": type(exc).__name__}))
            except Exception:  # noqa: BLE001
                pass
    return {"supported": True, "handled": handled, "errors": errors}
