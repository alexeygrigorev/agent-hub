"""Worktree guard: Hub session worktree locks + cleanup skip predicates.

Intake a0f01448 (worktree-guard, RCA 95c1862c/24638f4e): a session removed its
own git worktree while its intake was still open because nothing locked the
tree and every cleanup path treated the removal as legitimate. This module is
the single authority for:

- the Hub lock reason format ``hub-session <id> intake <intake-id|none>``;
- best-effort ``git worktree lock`` on start/adopt and release only when the
  session is terminal AND (no linked intake, or the linked intake has a DONE
  note / is archived);
- the shared ``should_skip_cleanup`` predicate used by every automated
  cleanup path (locked, live session, or session linked to an open intake);
- detecting a recreated worktree at a path whose original inode was deleted
  (stale `` (deleted)`` kernel label), so it is not treated as missing.

Stdlib only. All git calls are argv arrays (no shell). Tests use disposable
synthetic repos wholly inside the owned private test TMPDIR.
"""

from __future__ import annotations

import os
import json
from pathlib import Path
import re
import subprocess
import time
from contextlib import contextmanager
from typing import Any, Callable, Optional
from urllib.parse import urlencode

SESSION_ID_RE = re.compile(r"[0-9a-fA-F-]{8,64}\Z")
INTAKE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
GIT_TIMEOUT = 30.0
GIT_OUTPUT_LIMIT = 1024 * 1024

LOCK_REASON_RE = re.compile(
    r"\Ahub-session ([0-9a-fA-F-]{8,64}) intake ([A-Za-z0-9][A-Za-z0-9._:-]{0,127}|none)\Z"
)


def lock_reason(session_id: str, intake_id: Any) -> str:
    """Build the canonical Hub lock reason. Raises on malformed ids."""
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("session id is required")
    if intake_id is None or intake_id == "none" or intake_id == "":
        intake = "none"
    elif isinstance(intake_id, str) and INTAKE_ID_RE.fullmatch(intake_id):
        intake = intake_id
    else:
        raise ValueError("intake id is malformed")
    return "hub-session %s intake %s" % (session_id, intake)


def parse_lock_reason(reason: Any) -> Optional[tuple[str, Optional[str]]]:
    """Parse a Hub lock reason. Returns (session_id, intake_id|None) or None."""
    if not isinstance(reason, str):
        return None
    match = LOCK_REASON_RE.fullmatch(reason.strip())
    if not match:
        return None
    intake = match.group(2)
    return (match.group(1), None if intake == "none" else intake)


def _default_runner(argv: list[str], cwd: str, timeout: float = GIT_TIMEOUT):
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False, cwd=cwd
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("git call timed out; effect unknown") from exc
    except OSError as exc:
        raise RuntimeError("git executable failed: %s" % type(exc).__name__) from exc
    for stream in (proc.stdout, proc.stderr):
        if len(stream.encode("utf-8", "replace")) > GIT_OUTPUT_LIMIT:
            raise RuntimeError("git response exceeds bound; refusing")
    return proc


def _parse_worktree_list(text: str) -> list[dict[str, Any]]:
    """Parse `git worktree list --porcelain` (same grammar as session_cleanup)."""
    if not isinstance(text, str) or len(text.encode("utf-8", "replace")) > GIT_OUTPUT_LIMIT:
        raise ValueError("git worktree registry exceeds bound; refusing")
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    paths: set[str] = set()
    for line in [*text.splitlines(), ""]:
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            if current:
                raise ValueError("malformed git worktree record separator; refusing")
            path = line[len("worktree "):]
            if not os.path.isabs(path) or "\x00" in path or os.path.normpath(path) in paths:
                raise ValueError("invalid or duplicate git worktree path; refusing")
            paths.add(os.path.normpath(path))
            current = {"path": path, "branch": None, "bare": False, "locked": False, "reason": None}
            continue
        if not current:
            raise ValueError("git worktree field without a record; refusing")
        if line == "bare":
            current["bare"] = True
            continue
        if line == "detached":
            current["detached"] = True
            continue
        if line.startswith("locked"):
            current["locked"] = True
            rest = line[len("locked"):].strip()
            if rest:
                current["reason"] = rest
            continue
        if line.startswith("prunable"):
            current["prunable"] = True
            rest = line[len("prunable"):].strip()
            if rest:
                current["reason"] = current.get("reason") or rest
            continue
        key, _, value = line.partition(" ")
        if key == "HEAD" and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value) and "head" not in current:
            current["head"] = value
        elif key == "branch" and value.startswith("refs/") and not current["branch"]:
            current["branch"] = value
        else:
            raise ValueError("malformed or unsupported git worktree field; refusing")
    return entries


def list_entries(git_bin: str, path: str, runner: Optional[Callable] = None) -> list[dict[str, Any]]:
    """Return parsed worktree registry entries. Raises on git failure."""
    run = runner or _default_runner
    proc = run([git_bin, "worktree", "list", "--porcelain"], path, GIT_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError("git worktree list failed; refusing")
    return _parse_worktree_list(proc.stdout or "")


def find_entry(entries: Any, path: str) -> Optional[dict[str, Any]]:
    """Find the registry entry for `path` (normpath compare), or None."""
    if not isinstance(entries, list) or not isinstance(path, str) or not path:
        return None
    want = os.path.normpath(path)
    for entry in entries:
        if isinstance(entry, dict) and os.path.normpath(str(entry.get("path") or "")) == want:
            return entry
    return None


def is_registered_worktree(git_bin: str, path: str, runner: Optional[Callable] = None) -> Optional[bool]:
    """True/False when the registry is readable, None when unknown."""
    try:
        entries = list_entries(git_bin, path, runner)
    except Exception:
        return None
    return find_entry(entries, os.path.normpath(path)) is not None


def is_locked(git_bin: str, path: str, runner: Optional[Callable] = None) -> Optional[bool]:
    """True when the worktree entry is locked, False when registered-unlocked,
    None when the registry is unreadable or the path is unregistered."""
    try:
        entries = list_entries(git_bin, path, runner)
    except Exception:
        return None
    entry = find_entry(entries, os.path.normpath(path))
    if entry is None:
        return None
    return bool(entry.get("locked"))


def lock_worktree(
    git_bin: str,
    path: str,
    reason: str,
    runner: Optional[Callable] = None,
) -> bool:
    """Best-effort lock; returns True on success, False otherwise (never raises).

    Tries ``git worktree lock --reason`` first, falling back to a plain lock
    on older git versions that reject the flag.
    """
    try:
        if not isinstance(path, str) or not path or not os.path.isabs(path):
            return False
        if not isinstance(reason, str) or not reason or len(reason) > 512:
            return False
        run = runner or _default_runner
        proc = run([git_bin, "worktree", "lock", "--reason", reason, path], path, GIT_TIMEOUT)
        if proc.returncode == 0:
            return True
        combined = ((proc.stderr or "") + "\n" + (proc.stdout or "")).lower()
        if "unknown option" in combined or "unrecognized" in combined or "usage" in combined:
            fallback = run([git_bin, "worktree", "lock", path], path, GIT_TIMEOUT)
            return fallback.returncode == 0
        return False
    except Exception:
        return False


def unlock_worktree(git_bin: str, path: str, runner: Optional[Callable] = None) -> bool:
    """Best-effort unlock; returns True on success, False otherwise."""
    try:
        if not isinstance(path, str) or not path or not os.path.isabs(path):
            return False
        run = runner or _default_runner
        proc = run([git_bin, "worktree", "unlock", path], path, GIT_TIMEOUT)
        return proc.returncode == 0
    except Exception:
        return False


def ensure_locked(
    git_bin: str,
    path: str,
    session_id: str,
    intake_id: Any,
    runner: Optional[Callable] = None,
    *, owner_terminal: Optional[Callable[[str], bool]] = None,
) -> bool:
    """Lock the session worktree with the canonical Hub reason.

    A canonical lock for the same nonempty intake protects a retry only with
    fresh positive proof that its owner is terminal. Preserve the original
    owner; reuse grants no release permission. Unknown/live owners refuse.
    Returns False when the reason is malformed or git declines.
    """
    try:
        reason = lock_reason(session_id, intake_id)
    except (ValueError, TypeError):
        return False
    try:
        current = is_locked(git_bin, path, runner)
    except Exception:
        current = None
    if current is True:
        try:
            entry = find_entry(list_entries(git_bin, path, runner), path)
            if not entry:
                return False
            if entry.get("reason") == reason:
                return True
            owner = parse_lock_reason(entry.get("reason"))
            requested = parse_lock_reason(reason)
            return bool(owner and requested and requested[1] is not None
                        and owner[1] == requested[1] and owner_terminal is not None
                        and owner_terminal(owner[0]) is True)
        except Exception:
            return False
    if current is None:
        # Unknown registry state: attempt the lock anyway; git decides.
        pass
    return lock_worktree(git_bin, path, reason, runner)


def bound_worktree_root(path: str, git_bin="git", runner=None) -> Optional[str]:
    try:
        roots = [e["path"] for e in list_entries(git_bin, path, runner)
                 if (path == e["path"] or path.startswith(e["path"] + os.sep))
                 and os.path.isfile(os.path.join(e["path"], ".git"))]
        return max(roots, key=len) if roots else None
    except Exception:
        return None


def identity_directory() -> Path:
    """Shared host-only evidence location, outside disposable session checkouts."""
    return Path(os.environ.get("HUB_WORKTREE_IDENTITY_DIR") or
                Path.home() / ".local/state/agent-hub/worktree-identities")


def repository_identity(path: str, git_bin: str = "git", runner=None) -> Optional[dict]:
    """Identify the repository by canonical common-dir path and inode."""
    try:
        run = runner or _default_runner
        result = run([git_bin, "rev-parse", "--path-format=absolute", "--git-common-dir"], path, GIT_TIMEOUT)
        if result.returncode != 0:
            return None
        common = os.path.realpath(result.stdout.strip())
        stat = os.stat(common)
        if not os.path.isabs(common) or not os.path.isdir(common):
            return None
        return {"common_dir": common, "device": stat.st_dev, "inode": stat.st_ino}
    except Exception:
        return None


def remember_repository(launch_id: Any, path: str) -> bool:
    """Persist before native start; a replay cannot replace the original repo."""
    from . import private_fs
    if not isinstance(launch_id, str) or not SESSION_ID_RE.fullmatch(launch_id):
        return False
    identity = repository_identity(path)
    if identity is None:
        return False
    record = {"version": 1, "launch_id": launch_id, "workspace": os.path.normpath(path),
              "repository": identity}
    file = identity_directory() / (launch_id + ".json")
    try:
        with private_fs.exclusive_lock(file.with_suffix(".lock")):
            try:
                existing = json.loads(private_fs.read_bytes(file, 8192))
            except FileNotFoundError:
                existing = None
            if existing is not None:
                return existing == record
            private_fs.atomic_write(file, json.dumps(record).encode(), 8192)
        return True
    except Exception:
        return False


def _adoption_native_tuple(row: Any) -> Optional[dict]:
    if not isinstance(row, dict):
        return None
    native_id = row.get("native_id") or row.get("id")
    created = row.get("native_created_at_ms") or row.get("created_at_ms")
    if not isinstance(native_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", native_id) or type(created) is not int or created <= 0:
        return None
    values = {"native_id": native_id, "created_at_ms": created,
              **{key: row.get(key) for key in ("tag", "workspace", "engine")}}
    if not all(isinstance(values[key], str) and values[key] for key in ("tag", "workspace", "engine")):
        return None
    return values


def protect_operator_adoption(row: dict, intent: dict, git_bin: str = "git") -> Optional[dict]:
    """Effect only an explicit host-scoped intent, never a census label.

    Retain immutable exact-incarnation mapping before locking. A retry cannot
    replace a previous repository witness or take over a foreign lock.
    """
    from . import private_fs
    native = _adoption_native_tuple(row)
    if (not native or native != _adoption_native_tuple(intent.get("native"))
            or intent.get("session_id") != native["native_id"]
            or (row.get("state") or row.get("phase")) not in ("running", "starting")
            or not isinstance(intent.get("id"), str)
            or not re.fullmatch(r"[0-9a-fA-F-]{36}", intent["id"])):
        return None
    path = row.get("cwd") or row.get("workspace")
    if not isinstance(path, str) or not os.path.isdir(path):
        return None
    root = bound_worktree_root(path)
    # Unreadable linked registries are never mistaken for a non-worktree.
    if root is None:
        if not protect_bound_worktree(git_bin, path, native["native_id"], intent.get("intake_task_id")):
            return None
        return {"id": intent["id"], "native": native, "lock_verified": True, "worktree": False}
    if not remember_repository(intent["id"], path):
        return None
    file = identity_directory() / ("native-" + native["native_id"] + ".json")
    record = {"version": 1, "intent_id": intent["id"], "native": native}
    try:
        with private_fs.exclusive_lock(file.with_suffix(".lock")):
            try:
                prior = json.loads(private_fs.read_bytes(file, 8192))
            except FileNotFoundError:
                prior = None
            if prior is not None and prior != record:
                return None
            if prior is None:
                private_fs.atomic_write(file, json.dumps(record).encode(), 8192)
    except Exception:
        return None
    expected = expected_repository(row)
    if not expected or expected.get("repository") != repository_identity(path, git_bin):
        return None
    if not protect_bound_worktree(git_bin, path, native["native_id"], intent.get("intake_task_id")):
        return None
    return {"id": intent["id"], "native": native, "lock_verified": True, "worktree": True,
            "lock_path": expected["workspace"], "repository": expected["repository"]}


def expected_repository(row: Any) -> Optional[dict]:
    """Read pre-start evidence using the canonical native Hub launch tag."""
    from . import private_fs
    if not isinstance(row, dict):
        return None
    native = _adoption_native_tuple(row)
    if native:
        try:
            mapping = json.loads(private_fs.read_bytes(identity_directory() / ("native-" + native["native_id"] + ".json"), 8192))
            intent_id = mapping.get("intent_id")
            if mapping.get("version") == 1 and mapping.get("native") == native and isinstance(intent_id, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", intent_id):
                data = json.loads(private_fs.read_bytes(identity_directory() / (intent_id + ".json"), 8192))
                if data.get("version") == 1 and data.get("launch_id") == intent_id:
                    return data
        except Exception:
            pass
    tag = row.get("tag")
    match = re.fullmatch(r"(?:task-)?hub-([0-9a-fA-F-]{8,64})", tag or "")
    if not match:
        return None
    try:
        data = json.loads(private_fs.read_bytes(identity_directory() / (match[1] + ".json"), 8192))
        if data.get("version") != 1 or data.get("launch_id") != match[1]:
            return None
        return data
    except Exception:
        return None


def protect_bound_worktree(git_bin: str, path: str, session_id: Any, intake_id: Any,
                           runner: Optional[Callable] = None, *,
                           owner_terminal: Optional[Callable[[str], bool]] = None) -> bool:
    """Lock a linked checkout before starting a native session inside it.

    A main checkout or non-git workspace needs no worktree lock. For a linked
    checkout, missing identity, foreign locks and lock failures refuse start.
    """
    try:
        entries = list_entries(git_bin, path, runner)
    except Exception:
        # A .git file proves an intended linked tree even when git is broken.
        ceilings = {os.path.realpath(p) for p in os.environ.get("GIT_CEILING_DIRECTORIES", "").split(os.pathsep) if p}
        for parent in (Path(path), *Path(path).parents):
            if os.path.realpath(parent) in ceilings:
                break
            if (parent / ".git").is_file():
                return False
        return True
    roots = [e for e in entries if path == e["path"] or path.startswith(e["path"] + os.sep)]
    if not roots:
        return True
    root = max(roots, key=lambda e: len(e["path"]))["path"]
    if not os.path.isfile(os.path.join(root, ".git")):
        return True
    return ensure_locked(git_bin, root, session_id, intake_id, runner, owner_terminal=owner_terminal)


GUARD_PATH = "/api/sessions/worktree-guards"
GUARD_PAGE = 100


def guard_facts(request: Callable, **filters: Any) -> list:
    """Bounded worktree-guards read (intake 4d05302a).

    `session_id` / `native_id` / `intake_task_id` ask the Worker for just those
    sessions' facts. An old Worker ignores the parameters and returns its whole
    list, so callers still select their exact fact locally from the result.
    """
    query = urlencode([(k, str(v)) for k, v in filters.items() if v])
    return request("GET", GUARD_PATH + ("?" + query if query else ""))["sessions"]


def reuse_stopped_intake_lock(git_bin: str, path: str, session_id: str,
                             intake_id: Any, request: Callable) -> bool:
    """Reuse protection without unlocking a stopped predecessor's same-intake tree.

    The host-scoped ledger must prove both identities and predecessor exit.
    The original lock stays in place; release is fenced by all live sessions
    linked to the intake. Missing evidence or a foreign/manual lock refuses.
    """
    try:
        lock_reason(session_id, intake_id)
        if not intake_id or intake_id == "none":
            return False
        root = bound_worktree_root(path, git_bin)
        entry = find_entry(list_entries(git_bin, root), root) if root else None
        owner = parse_lock_reason(entry.get("reason")) if entry else None
        if not entry or not entry.get("locked") or not owner or owner[1] != intake_id:
            return False
        facts = guard_facts(request, session_id=",".join([session_id, owner[0]]))
        successor = [f for f in facts if f.get("session_id") == session_id and f.get("intake_task_id") == intake_id]
        previous = [f for f in facts if f.get("session_id") == owner[0] and f.get("intake_task_id") == intake_id]
        if len(successor) != 1 or successor[0].get("terminal") is not False or len(previous) != 1:
            return False
        fact = previous[0]
        expected = expected_repository(fact)
        if (fact.get("terminal") is not True or fact.get("native_terminal") is not True
                or not expected or expected.get("workspace") != root
                or expected.get("repository") != repository_identity(root, git_bin)):
            return False
        # Re-read after remote I/O; never silently accept a replaced lock.
        current = find_entry(list_entries(git_bin, root), root)
        return bool(current and current.get("locked") and current.get("reason") == entry.get("reason"))
    except Exception:
        return False


@contextmanager
def _ownership_guard(path):
    """Serialize successor ownership with lifecycle unlocks on this host."""
    import hashlib
    from . import private_fs
    key = hashlib.sha256(os.path.realpath(path).encode()).hexdigest()
    with private_fs.exclusive_lock(identity_directory() / ('ownership-' + key + '.lock')):
        yield


def protect_successor_worktree(request, git_bin, path, session_id, intake_id):
    """Keep a same-intake checkout locked while replacing a terminal owner.

    A fresh host-only Hub response must prove the previous Hub session AND
    its native incarnation terminal. The saved repository identity must also
    match. Manual/foreign locks, live owners and unknown evidence fail closed.
    The Git lock file is atomically replaced, never unlinked/unlocked.
    """
    from . import private_fs
    import stat
    import uuid
    try:
        reason = lock_reason(session_id, intake_id)
        if not intake_id or intake_id == 'none' or os.name != 'posix':
            return False
        root = bound_worktree_root(path, git_bin)
        if root is None:
            return False
        with _ownership_guard(root):
            entry = find_entry(list_entries(git_bin, root), root)
            owner = parse_lock_reason(entry.get('reason')) if entry else None
            if not entry or not entry.get('locked') or not owner or owner[1] != intake_id:
                return False
            if owner[0] == session_id:
                return True
            facts = guard_facts(request, session_id=owner[0])
            matches = [f for f in facts if isinstance(f, dict) and f.get('session_id') == owner[0]]
            if len(matches) != 1:
                return False
            fact = matches[0]
            if fact.get('terminal') is not True or fact.get('native_terminal') is not True:
                return False
            expected = expected_repository(fact)
            identity = repository_identity(root, git_bin)
            saved_path = expected.get('workspace') if expected else None
            if (not isinstance(saved_path, str) or not identity
                    or expected.get('repository') != identity
                    or bound_worktree_root(saved_path, git_bin) != root):
                return False
            proc = _default_runner([git_bin, 'rev-parse', '--absolute-git-dir'], root, GIT_TIMEOUT)
            gitdir = Path(proc.stdout.strip())
            if proc.returncode or not gitdir.is_absolute() or gitdir.parent != Path(identity['common_dir']) / 'worktrees':
                return False
            lock = gitdir / 'locked'
            with private_fs.parent(lock, private=False) as directory:
                current = os.stat('locked', dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or current.st_uid != os.getuid():
                    return False
                if private_fs.read_bytes(lock, 1024, private=False).decode().strip() != entry['reason']:
                    return False
                temporary = 'locked.' + uuid.uuid4().hex + '.tmp'
                fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write((reason + '\n').encode())
                        stream.flush()
                        os.fsync(stream.fileno())
                    # Fence replacement against a changed/deleted Git lock.
                    latest = os.stat('locked', dir_fd=directory, follow_symlinks=False)
                    if (latest.st_dev, latest.st_ino, latest.st_mtime_ns) != (current.st_dev, current.st_ino, current.st_mtime_ns):
                        return False
                    os.replace(temporary, 'locked', src_dir_fd=directory, dst_dir_fd=directory)
                    os.fsync(directory)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
            return find_entry(list_entries(git_bin, root), root).get('reason') == reason
    except Exception:
        return False


def release_session_lock(git_bin: str, path: str, session_id: Any,
                         *, terminal: bool, intake_state: str,
                         runner: Optional[Callable] = None) -> bool:
    """Release only this session's canonical Hub lock after its release gate.

    Manual locks, other sessions' locks and unreadable registries stay intact.
    Cleanup itself never unlocks; this is a session lifecycle operation.
    """
    if not can_unlock(terminal, intake_state):
        return False
    try:
        entries = list_entries(git_bin, path, runner)
        roots = [entry for entry in entries
                 if path == entry["path"] or path.startswith(entry["path"] + os.sep)]
        entry = max(roots, key=lambda entry: len(entry["path"])) if roots else None
        if not entry:
            return False
        with _ownership_guard(entry['path']):
            entry = find_entry(list_entries(git_bin, path, runner), entry['path'])
            owner = parse_lock_reason(entry.get("reason")) if entry else None
            if not entry or not entry.get("locked") or not owner or owner[0] != session_id:
                return False
            return unlock_worktree(git_bin, entry["path"], runner)
    except Exception:
        return False


def can_unlock(session_terminal: Any, intake_state: Any) -> bool:
    """Release gate (AC1): terminal AND (no intake, or DONE/archived intake).

    ``intake_state`` is one of ``none`` (no linked intake), ``open``,
    ``done`` (DONE note with live proof), ``archived``. Anything else fails
    closed (False).
    """
    if session_terminal is not True:
        return False
    return intake_state in ("none", "done", "archived")


def should_skip_cleanup(
    locked: Any = False,
    live: Any = False,
    linked_open_intake: Any = False,
) -> bool:
    """Shared automated-cleanup skip predicate (AC2).

    Skips when the worktree is locked, belongs to a live session, or belongs
    to a session linked to an open intake. Truthy inputs skip; unknown (None)
    is treated as False here — callers that must fail closed pass True.
    """
    return bool(locked) or bool(live) or bool(linked_open_intake)


def _strip_deleted_suffix(path: str) -> str:
    if isinstance(path, str) and path.endswith(" (deleted)") and not os.path.exists(path):
        return path[: -len(" (deleted)")]
    return path


def detect_recreated(
    cwd_raw: Any,
    git_bin: str = "git",
    runner: Optional[Callable] = None,
    *, expected: Optional[dict] = None,
) -> bool:
    """True when a `` (deleted)`` cwd path now holds a valid worktree (AC5).

    The kernel keeps the deleted inode label on the process cwd even after
    the same path is recreated. A literal same-named path is never treated
    as deleted; recreation requires registry proof and the repository identity
    captured before this session started.
    """
    if not isinstance(cwd_raw, str) or not cwd_raw.endswith(" (deleted)"):
        return False
    stripped = cwd_raw[: -len(" (deleted)")]
    if not stripped or not os.path.isabs(stripped):
        return False
    if not os.path.isdir(stripped):
        return False
    # Registry proof: the stripped path is a registered worktree.
    try:
        entries = list_entries(git_bin, stripped, runner)
    except Exception:
        entries = None
    if entries is not None and bound_worktree_root(stripped, git_bin, runner) is not None:
        if expected is None:
            return False
        return (expected.get("workspace") == os.path.normpath(stripped) and
                expected.get("repository") == repository_identity(stripped, git_bin, runner))
    return False


def classify_stale_path(
    cwd_raw: Any,
    git_bin: str = "git",
    runner: Optional[Callable] = None,
    *, expected: Optional[dict] = None,
) -> str:
    """Return ``present`` | ``missing`` | ``cwd_stale_path_recreated``.

    ``present``: the path exists (including a literal `` (deleted)`` path).
    ``cwd_stale_path_recreated``: the kernel label is stale — the stripped
    path now holds a valid worktree of the repo. ``missing`` otherwise.
    """
    if not isinstance(cwd_raw, str) or not cwd_raw:
        return "missing"
    if os.path.exists(cwd_raw):
        return "present"
    if cwd_raw.endswith(" (deleted)") and detect_recreated(cwd_raw, git_bin, runner, expected=expected):
        return "cwd_stale_path_recreated"
    return "missing"


def cleanup_permitted(row: Any, request: Callable, git_bin: str = "git") -> bool:
    """Fresh, exact positive authority for automated native/tree cleanup.

    A dead leader alone is not a terminal Hub session. Missing identity,
    unreadable ledgers/registry, and failed requests always retain resources.
    """
    try:
        if not isinstance(row, dict) or row.get("phase") not in ("exited", "dead", "pruned", "forgotten"):
            return False
        native_id = row.get("native_id") or row.get("id")
        incarnation = row.get("native_created_at_ms", row.get("created_at_ms"))
        if not native_id or type(incarnation) is not int or incarnation <= 0:
            return False
        facts = guard_facts(request, native_id=native_id)
        matches = [fact for fact in facts if
                   fact.get("native_id") == native_id and
                   fact.get("native_created_at_ms") == incarnation and
                   fact.get("tag") == row.get("tag") and
                   fact.get("workspace") == row.get("workspace")]
        if len(matches) != 1:
            return False
        fact = matches[0]
        if fact.get("terminal") is not True or fact.get("native_terminal") is not True or fact.get("linked_intake_open") is not False or fact.get("protected") is not False:
            return False
        for path in {row.get("workspace"), row.get("cwd") or row.get("workspace")}:
            if not isinstance(path, str) or not path or is_locked(git_bin, path) is not False:
                return False
        return True
    except Exception:
        return False


def protect_adoption(row: dict, session: dict, git_bin: str = "git",
                     *, request: Optional[Callable] = None) -> bool:
    """Lock actual adopted native binding; never invent pre-start evidence."""
    path = row.get("cwd") or row.get("workspace")
    if not isinstance(path, str) or not os.path.isdir(path):
        return False
    expected = expected_repository(row)
    if expected and expected.get("repository") != repository_identity(path, git_bin):
        return False
    if not protect_bound_worktree(git_bin, path, session.get("id"), session.get("intake_task_id")):
        # The launch can legitimately retain its stopped predecessor's lock.
        # Binding must consume the same authenticated same-intake proof too;
        # otherwise a running successor remains permanently unbound.
        if request is None or not reuse_stopped_intake_lock(
                git_bin, path, session.get("id"), session.get("intake_task_id"), request):
            return False
    if bound_worktree_root(path) is None:
        return True
    return bool(expected and expected.get("repository") == repository_identity(path, git_bin)
                and expected.get("workspace") == os.path.normpath(path))


_reconcile_offset = 0
_reconcile_cursor = None

def reconcile_terminal_locks(request: Callable, git_bin: str = "git") -> int:
    """Retry non-destructive lifecycle unlocks, including late DONE/archive.

    Server terminal + fresh intake gate, original repo evidence, and exact
    lock ownership are all necessary. Manual/foreign/unknown locks remain.
    """
    global _reconcile_offset, _reconcile_cursor
    released = 0
    try:
        result = request("GET", GUARD_PATH + "?" + urlencode(
            [("limit", GUARD_PAGE)] + ([("cursor", _reconcile_cursor)] if _reconcile_cursor else [])))
        facts = result["sessions"]
        # A bounded Worker pages terminal sessions with next_cursor (null at
        # the end: the next pass wraps). An old Worker has no such key and
        # returns everything: rotate through that list as before.
        paged = "next_cursor" in result
        if paged:
            _reconcile_cursor = result["next_cursor"]
    except Exception:
        return 0
    deadline = time.monotonic() + 20
    if not isinstance(facts, list) or not facts:
        return 0
    offset = 0 if paged else _reconcile_offset % len(facts)
    for index in range(len(facts)):
        if time.monotonic() >= deadline:
            break
        position = (offset + index) % len(facts)
        if not paged:
            _reconcile_offset = position + 1
        fact = facts[position]
        if not isinstance(fact, dict) or fact.get("terminal") is not True or fact.get("native_terminal") is not True or fact.get("linked_intake_open") is not False or fact.get("lock_release_permitted") is False or (fact.get("protected") is not False and fact.get("lock_release_permitted") is not True):
            continue
        expected = expected_repository(fact)
        path = expected.get("workspace") if expected else None
        if not isinstance(path, str) or expected.get("repository") != repository_identity(path, git_bin):
            continue
        if release_session_lock(git_bin, path, fact.get("session_id"), terminal=True, intake_state="done"):
            released += 1
    return released
