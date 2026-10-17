"""M15d host cleanup executor: owned Git worktree/branch removal after confirmed stop.

Single-authority split: the TypeScript server (`src/session_control.ts` +
`src/session_lifecycle_registry.ts`) is the ONE classifier/planner and the
M12 command store is the ONE custody record (enqueue/claim/ack). This module
owns ONLY the host-side worktree/branch effect guards plus a durable journal:

- binding/owner shape revalidation reuses
  :func:`agent_hub_host.session_lifecycle.validate_cleanup_target`
  (no second classifier);
- caller custody reuses :class:`agent_hub_host.session_executor.CallerCustody`
  (no second custody checker);
- confirmed native stop reuses the existing M12 claim/effect/ack path
  (:mod:`agent_hub_host.session_receiver`, containment-confirmed
  :func:`agent_hub_host.native_sessions.stop_session`; a missing catalog
  entry is never exit proof, no tag-only PID kill);
- this module adds ONLY: trusted cleanup-root confinement, dedicated
  registered-worktree checks, clean/lock/exclusive-branch guards, live-use
  refusal, FRESH `origin/main` ancestry proof via `ls-remote` (network/unknown
  fails closed to listed-only), ordinary `git worktree remove` then
  `git branch -d` (no `--force`, no reset), and a durable per-target journal
  so restart/lost-response never repeats an unsafe deletion.

Stdlib only. All git calls are argv arrays (no shell). Private receipts and
the owned bounded native history live OUTSIDE the removed tree (journal_dir
is never under a cleanup root), so removal preserves them by construction.
Real worktrees, histories, and sources are never touched by tests: tests use
disposable synthetic repos wholly inside the owned private test TMPDIR.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from . import private_fs
from .native_sessions import SessionControlError, SessionUncertain
from .session_executor import CallerCustody
from .session_lifecycle import validate_cleanup_target

JOURNAL_LIMIT = 64 * 1024
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,127}\Z")
MAX_ROOTS = 16
GIT_TIMEOUT = 30.0


def _journal_path(journal_dir: str, name: str) -> str:
    if not isinstance(journal_dir, str) or not Path(journal_dir).is_absolute():
        raise SessionControlError("journal_dir must be an absolute path")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,128}", name):
        raise SessionControlError("Invalid journal name")
    return os.path.join(journal_dir, name + ".json")


def _read_journal(path: str) -> Optional[dict[str, Any]]:
    import json

    try:
        raw = private_fs.read_bytes(path, JOURNAL_LIMIT)
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SessionUncertain(f"Journal corrupt; reconcile before retry: {path}") from exc
    if not isinstance(data, dict):
        raise SessionUncertain(f"Journal malformed; reconcile before retry: {path}")
    return data


def _write_journal(path: str, record: dict[str, Any]) -> None:
    import json

    raw = json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")
    private_fs.atomic_write(path, raw, JOURNAL_LIMIT)


def _locked(path: str):
    return private_fs.exclusive_lock(path + ".lock")


def _lock_held(path: str):
    try:
        return _locked(path)
    except BlockingIOError as exc:
        raise SessionUncertain(
            "Cleanup intent locked by a concurrent process; reconcile, do not duplicate"
        ) from exc
    except OSError as exc:
        raise SessionUncertain("Cleanup intent lock unavailable; reconcile, do not duplicate") from exc


def canonical_roots(roots: Any) -> list[str]:
    """Validate the explicit trusted host cleanup-root allowlist (plumbing only).

    Returns canonical absolute roots. Anything else raises (fail closed):
    non-list, empty, too many, non-string, non-absolute, null bytes.
    """
    if not isinstance(roots, list) or not roots or len(roots) > MAX_ROOTS:
        raise SessionControlError("cleanup_roots must be a non-empty list (max %d)" % MAX_ROOTS)
    canonical: list[str] = []
    for root in roots:
        if not isinstance(root, str) or not root or "\x00" in root or not os.path.isabs(root):
            raise SessionControlError("cleanup_roots entries must be absolute paths")
        norm = os.path.normpath(root)
        if norm in canonical:
            raise SessionControlError("cleanup_roots entries must be unique")
        canonical.append(norm)
    return canonical


def resolve_owned_worktree(candidate: Any, roots: list[str]) -> str:
    """Confinement check: canonical realpath strictly under one trusted root.

    Refuses symlinks/realpath escape (abspath must already equal realpath),
    shared roots themselves (must be strictly under), and non-absolute input.
    Returns the canonical realpath. No I/O beyond readlink.
    """
    if not isinstance(candidate, str) or not candidate or "\x00" in candidate:
        raise SessionControlError("worktree path is required")
    if not os.path.isabs(candidate):
        raise SessionControlError("worktree path must be absolute")
    absolute = os.path.normpath(candidate)
    realpath = os.path.realpath(absolute)
    if realpath != absolute:
        raise SessionControlError("Refusing symlinked worktree path (realpath escape)")
    for root in roots:
        if realpath != root and realpath.startswith(root + os.sep):
            return realpath
    raise SessionControlError("worktree escapes trusted cleanup roots")


def _default_runner(argv: list[str], cwd: str, timeout: float = GIT_TIMEOUT):
    if not isinstance(argv, list) or any(not isinstance(a, str) or not a for a in argv):
        raise SessionControlError("git argv must be a non-empty string list")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        raise SessionUncertain("git call timed out; effect unknown, reconcile") from exc
    except OSError as exc:
        raise SessionControlError(f"git executable failed: {type(exc).__name__}") from exc
    for stream in (proc.stdout, proc.stderr):
        if len(stream.encode("utf-8", "replace")) > 1024 * 1024:
            raise SessionUncertain("git response exceeds bound; effect unknown")
    return proc


def _parse_worktree_list(text: str) -> list[dict[str, Any]]:
    """Parse `git worktree list --porcelain` into entries (deterministic, bounded)."""
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in text.splitlines()[:500]:
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            if current:
                entries.append(current)
                current = {}
            current = {"path": line[len("worktree "):], "branch": None, "bare": False, "locked": False}
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):]
        elif line == "bare":
            current["bare"] = True
        elif line == "locked" or line.startswith("locked "):
            current["locked"] = True
    if current:
        entries.append(current)
    return entries


def _git_common_dir(git_bin: str, path: str, runner: Callable) -> str:
    proc = runner([git_bin, "rev-parse", "--git-common-dir"], path, GIT_TIMEOUT)
    if proc.returncode != 0:
        raise SessionControlError("not a git repository; refusing")
    common = (proc.stdout or "").strip().splitlines()
    if not common or not common[0]:
        raise SessionControlError("git common dir unknown; refusing")
    candidate = common[0]
    resolved = candidate if os.path.isabs(candidate) else os.path.normpath(os.path.join(path, candidate))
    if os.path.realpath(resolved) != resolved:
        raise SessionControlError("Refusing symlinked git common dir")
    return resolved


def check_registered_worktree(
    git_bin: str, realpath: str, branch: str, *, runner: Callable
) -> dict[str, Any]:
    """Verify the target is a dedicated REGISTERED worktree for exactly `branch`.

    Refuses: unregistered paths, bare repos, shared main checkouts
    (`.git` is a directory, not a worktree file), detached HEAD, branch
    mismatch, locked worktrees, and branches checked out elsewhere.
    Returns the parsed entries + common dir for later steps.
    """
    if not isinstance(branch, str) or not BRANCH.fullmatch(branch) or branch.startswith(("-", ".", "/")):
        raise SessionControlError("Invalid branch name")
    if ".." in branch.split("/"):
        raise SessionControlError("Invalid branch name")
    dotgit = os.path.join(realpath, ".git")
    if not os.path.lexists(dotgit):
        raise SessionControlError("not a git worktree (no .git); refusing")
    if os.path.isdir(dotgit) and not os.path.islink(dotgit):
        raise SessionControlError("Refusing shared main checkout (only dedicated worktrees)")
    proc = runner([git_bin, "worktree", "list", "--porcelain"], realpath, GIT_TIMEOUT)
    if proc.returncode != 0:
        raise SessionControlError("git worktree list failed; refusing")
    entries = _parse_worktree_list(proc.stdout or "")
    mine = [e for e in entries if os.path.normpath(e["path"]) == realpath]
    if not mine:
        raise SessionControlError("not a registered git worktree; refusing")
    if mine[0].get("bare"):
        raise SessionControlError("Refusing bare repository worktree")
    if mine[0].get("locked"):
        raise SessionControlError("worktree locked; refusing")
    want = "refs/heads/" + branch
    if not mine[0].get("branch"):
        raise SessionControlError("detached HEAD worktree; refusing (named branch required)")
    if mine[0]["branch"] != want:
        raise SessionControlError("worktree branch mismatch; refusing")
    for other in entries:
        if os.path.normpath(other["path"]) != realpath and other.get("branch") == want:
            raise SessionControlError("branch checked out in another worktree; refusing")
    return {"entries": entries, "common_dir": _git_common_dir(git_bin, realpath, runner)}


def check_clean(git_bin: str, realpath: str, common_dir: str, *, runner: Callable) -> None:
    """Refuse any dirty/index/untracked content and git locks."""
    proc = runner([git_bin, "status", "--porcelain=v1", "--untracked-files=all"], realpath, GIT_TIMEOUT)
    if proc.returncode != 0:
        raise SessionControlError("git status failed; refusing")
    if (proc.stdout or "").strip():
        raise SessionControlError("dirty worktree (including untracked/index); refusing")
    if os.path.exists(os.path.join(common_dir, "index.lock")):
        raise SessionControlError("git index locked; refusing")


def check_no_live_use(realpath: str, live_paths: Any) -> None:
    """Refuse when another live session's cwd/worktree uses the target tree."""
    if live_paths is None:
        return
    if not isinstance(live_paths, (list, tuple)):
        raise SessionControlError("live_paths must be a list")
    for raw in live_paths:
        if not isinstance(raw, str) or not raw:
            continue
        live = os.path.normpath(raw)
        if live == realpath or live.startswith(realpath + os.sep) or realpath.startswith(live + os.sep):
            raise SessionControlError("another live session uses this worktree; refusing")


def verify_merged_fresh(git_bin: str, realpath: str, *, runner: Callable) -> str:
    """Prove HEAD is entirely reachable from FRESH `origin/main`.

    Uses `ls-remote` (network read, no local mutation). Network/unknown fails
    closed to listed-only (SessionControlError, never deletion). Returns the
    fresh main SHA on success.
    """
    proc = runner([git_bin, "ls-remote", "origin", "refs/heads/main"], realpath, GIT_TIMEOUT)
    if proc.returncode != 0:
        raise SessionControlError("remote_unavailable: origin/main unreachable; listed-only")
    sha = ""
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "refs/heads/main" and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
            sha = parts[0]
            break
    if not sha:
        raise SessionControlError("remote_unavailable: origin/main unknown; listed-only")
    ancestor = runner([git_bin, "merge-base", "--is-ancestor", "HEAD", sha], realpath, GIT_TIMEOUT)
    if ancestor.returncode == 0:
        return sha
    if ancestor.returncode == 1:
        raise SessionControlError("unmerged commits: HEAD not reachable from fresh origin/main; listed-only")
    raise SessionControlError("ancestry_unknown: merge-base failed; listed-only")


def _branch_absent(git_bin: str, cwd: str, branch: str, *, runner: Callable) -> bool:
    proc = runner([git_bin, "branch", "--list", branch], cwd, GIT_TIMEOUT)
    if proc.returncode != 0:
        return False
    return branch not in (proc.stdout or "")


def remove_owned_worktree(
    *,
    custody: CallerCustody,
    journal_dir: str,
    machine_id: str,
    native_id: str,
    expected_owner: str,
    worktree: str,
    branch: str,
    cleanup_roots: Any,
    live_paths: Any = (),
    command_id: Optional[str] = None,
    git_bin: str = "git",
    runner: Optional[Callable] = None,
) -> dict[str, Any]:
    """Remove ONE owned dedicated worktree + branch after confirmed stop.

    Fresh guards run on EVERY call (no stale approval): binding shape via
    :func:`validate_cleanup_target`, custody labels, root confinement,
    registered-worktree exclusivity, clean+lunlocked, no live use, and FRESH
    ancestry proof. Ordinary `git worktree remove` runs first, then
    `git branch -d` only after the remove verified. No `--force`, no reset.

    Durable journal (`cleanup-<machine>-<nativehex>.json`): terminal
    `tree_removed`/`refused` outcomes replay without re-running git;
    `uncertain` (a mutating call failed mid-effect) reconciles read-only
    before any retry and never blind-repeats a deletion. Returns the record
    with `replayed`/`reconciled` markers; raises SessionControlError for
    refusals is NEVER done — refusals are returned as data
    (`state: refused`), never thrown, so callers report kept reasons.
    """
    if not isinstance(custody, CallerCustody):
        raise SessionControlError("Caller custody is required")
    custody.validated()
    if not isinstance(git_bin, str) or not git_bin or "\x00" in git_bin:
        raise SessionControlError("git binary is required")
    run = runner or _default_runner
    gate = validate_cleanup_target({
        "expected_machine_id": machine_id,
        "expected_native_id": native_id,
        "expected_owner": expected_owner,
        "observed_machine_id": machine_id,
        "observed_native_id": native_id,
        "observed_owner": expected_owner,
        "busy_or_draft_or_pending": False,
        "contained": True,
    })
    if not gate.get("ok"):
        raise SessionControlError("cleanup binding refused: %s" % gate.get("error", "unknown"))
    if custody.machine_id != machine_id:
        raise SessionControlError("custody machine must own the cleanup target")
    roots = canonical_roots(cleanup_roots)
    hexid = re.sub(r"[^0-9a-fA-F]", "", str(native_id or ""))[:32]
    if not hexid:
        raise SessionControlError("native id is required")
    journal_file = _journal_path(journal_dir, "cleanup-%s-%s" % (machine_id, hexid))
    Path(journal_dir).mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(journal_dir, 0o700)
    except OSError:
        pass
    with _lock_held(journal_file):
        prior = _read_journal(journal_file)
        if prior is not None and prior.get("state") in ("tree_removed", "refused", "uncertain"):
            if (prior.get("worktree"), prior.get("branch")) != (worktree, branch):
                raise SessionControlError("cleanup idempotency conflict (no partial mutation)")
        if prior is not None and prior.get("state") == "tree_removed":
            return {**prior, "replayed": True}
        # A journaled `refused` verdict is audit, not a lock: guards below
        # re-run fresh on every call (read-only until the guarded remove),
        # so an operator-fixed tree (committed dirt, merged branch) converges
        # on retry, while a lost refused response replays the same verdict.
        # Only `tree_removed` terminally replays; mutations never repeat.
        # Resume a half-finished removal: the worktree is already gone
        # (verified below via worktree list); only the branch delete remains.
        # Guards are NOT re-run against a missing tree — the pre-removal
        # proof lives in this journal (fresh ancestry + clean + exclusive).
        resume_branch_only = (
            isinstance(prior, dict)
            and prior.get("state") == "uncertain"
            and prior.get("worktree_removed") is True
            and prior.get("worktree") == worktree
            and prior.get("branch") == branch
            and isinstance(prior.get("common_dir"), str)
        )
        if resume_branch_only:
            common_dir = str(prior["common_dir"])
            fresh_sha = str(prior.get("fresh_origin_main") or "unknown")

            def _record(state: str, reason: str, **extra: Any) -> dict[str, Any]:
                record = {
                    "op": "cleanup-tree",
                    "machine_id": machine_id,
                    "native_id": native_id,
                    "expected_owner": expected_owner,
                    "caller_actor": custody.actor,
                    "worktree": worktree,
                    "realpath": prior.get("realpath"),
                    "branch": branch,
                    "common_dir": common_dir,
                    "fresh_origin_main": fresh_sha,
                    "state": state,
                    "reason": str(reason)[:300],
                    **({"command_id": command_id} if command_id else {}),
                    **extra,
                }
                _write_journal(journal_file, record)
                return {**record}

            try:
                relist = run([git_bin, "worktree", "list", "--porcelain"], common_dir, GIT_TIMEOUT)
                still_there = relist.returncode == 0 and any(
                    os.path.normpath(e["path"]) == os.path.normpath(str(prior.get("realpath") or ""))
                    for e in _parse_worktree_list(relist.stdout or ""))
            except (SessionControlError, SessionUncertain):
                still_there = True
            if still_there:
                return _record("uncertain", "worktree still registered after uncertain remove; reconcile")
            try:
                deleted = run([git_bin, "branch", "-d", branch], common_dir, GIT_TIMEOUT)
            except SessionUncertain as exc:
                raise SessionUncertain("branch delete uncertain; reconcile") from exc
            if deleted.returncode != 0 and not _branch_absent(git_bin, common_dir, branch, runner=run):
                return _record("refused", "branch delete declined: %s" % ((deleted.stderr or "").strip().splitlines() or ["git declined"])[-1][:200], worktree_removed=True)
            return _record("tree_removed", "owned worktree removed; branch deleted after fresh ancestry proof", worktree_removed=True, branch_deleted=True, reconciled=True)
        realpath: Optional[str] = None
        common_dir: Optional[str] = None
        fresh_sha: Optional[str] = None
        guard_error: Optional[str] = None
        try:
            realpath = resolve_owned_worktree(worktree, roots)
            info = check_registered_worktree(git_bin, realpath, branch, runner=run)
            common_dir = info["common_dir"]
            check_clean(git_bin, realpath, common_dir, runner=run)
            check_no_live_use(realpath, live_paths)
            fresh_sha = verify_merged_fresh(git_bin, realpath, runner=run)
        except (SessionControlError, SessionUncertain) as exc:
            # Guard refusals are returned as data (never thrown): the observed
            # kept reason is journaled for audit and reported; nothing was
            # removed or mutated. Read-only timeouts fail closed here too
            # (listed-only); a later retry re-evaluates fresh.
            guard_error = str(exc)[:300]
        if guard_error is not None or not realpath or not common_dir or not fresh_sha:
            refused = {
                "op": "cleanup-tree",
                "machine_id": machine_id,
                "native_id": native_id,
                "expected_owner": expected_owner,
                "caller_actor": custody.actor,
                "worktree": worktree,
                "realpath": realpath,
                "branch": branch,
                "common_dir": common_dir,
                "fresh_origin_main": fresh_sha,
                "state": "refused",
                "reason": guard_error or "guard evaluation failed; listed-only",
                **({"command_id": command_id} if command_id else {}),
            }
            _write_journal(journal_file, refused)
            return {**refused}

        def _record(state: str, reason: str, **extra: Any) -> dict[str, Any]:
            record = {
                "op": "cleanup-tree",
                "machine_id": machine_id,
                "native_id": native_id,
                "expected_owner": expected_owner,
                "caller_actor": custody.actor,
                "worktree": worktree,
                "realpath": realpath,
                "branch": branch,
                "common_dir": common_dir,
                "fresh_origin_main": fresh_sha,
                "state": state,
                "reason": str(reason)[:300],
                **({"command_id": command_id} if command_id else {}),
                **extra,
            }
            _write_journal(journal_file, record)
            return {**record}

        # Ordinary worktree remove (no --force). A refusal here means git
        # itself declined (e.g. raced dirty): report kept, never forced.
        # (A half-finished removal resumed above and already returned.)
        try:
            removed = run([git_bin, "worktree", "remove", realpath], common_dir, GIT_TIMEOUT)
        except SessionUncertain as exc:
            _write_journal(journal_file, {
                "op": "cleanup-tree", "machine_id": machine_id, "native_id": native_id,
                "expected_owner": expected_owner, "caller_actor": custody.actor,
                "worktree": worktree, "realpath": realpath, "branch": branch,
                "common_dir": common_dir,
                "fresh_origin_main": fresh_sha, "state": "uncertain",
                "reason": "worktree remove uncertain: %s" % type(exc).__name__,
                **({"command_id": command_id} if command_id else {}),
            })
            raise
        if removed.returncode != 0:
            # Reconcile: verify whether the tree is actually gone before
            # deciding refused vs uncertain.
            try:
                relist = run([git_bin, "worktree", "list", "--porcelain"], common_dir, GIT_TIMEOUT)
                gone = relist.returncode == 0 and all(
                    os.path.normpath(e["path"]) != realpath
                    for e in _parse_worktree_list(relist.stdout or ""))
            except (SessionControlError, SessionUncertain):
                gone = False
            if not gone:
                return _record("refused", "worktree remove declined: %s" % ((removed.stderr or "").strip().splitlines() or ["git declined"])[-1][:200])
            _write_journal(journal_file, {
                "op": "cleanup-tree", "machine_id": machine_id, "native_id": native_id,
                "expected_owner": expected_owner, "caller_actor": custody.actor,
                "worktree": worktree, "realpath": realpath, "branch": branch,
                "common_dir": common_dir,
                "fresh_origin_main": fresh_sha, "state": "uncertain",
                "reason": "worktree removed; branch delete pending",
                "worktree_removed": True,
                **({"command_id": command_id} if command_id else {}),
            })
        # Ordinary branch delete (no -D/--force), only after the remove.
        try:
            deleted = run([git_bin, "branch", "-d", branch], common_dir, GIT_TIMEOUT)
        except SessionUncertain as exc:
            raise SessionUncertain("branch delete uncertain; reconcile") from exc
        if deleted.returncode != 0 and not _branch_absent(git_bin, common_dir, branch, runner=run):
            return _record("refused", "branch delete declined: %s" % ((deleted.stderr or "").strip().splitlines() or ["git declined"])[-1][:200], worktree_removed=True)
        return _record("tree_removed", "owned worktree removed; branch deleted after fresh ancestry proof", worktree_removed=True, branch_deleted=True)
