"""Hub-run disk floor janitor (intake fcc31e56): keep root free space above a floor.

One bounded pass (``DiskJanitor.run_once``) runs from the host agent's disk
monitor thread on a cadence (default 15 min, 2 min while below the floor):

1. Finished workspaces (always): registered git worktrees under the configured
   workspace roots whose sessions are terminal are removed after a short grace
   period, only when they are clean, have no unpushed commits, their HEAD is
   reachable from a remote ref and every linked intake is DONE. Such a tree that
   also holds non-regenerable ignored data (``.local`` evidence) has that data
   archived to the network disk (verified, kept) before removal, under pressure.
   Clean unpushed commits are bundled to the network disk and kept in their
   worktree. Uncommitted or no-DONE trees are never removed.
2. Engine caches and test scratch (always, age-bounded): entries of configured
   cache directories and scratch globs untouched for the configured age.

Safety rules (each one fails closed, so an unknown fact keeps the data):

- never a protected path (``~/prod``, backups, exports, dumps, the network
  disk) nor any ancestor of one;
- never a tree with a live aplexer session, a live Hub session, an open linked
  intake, a git lock, or any process whose cwd/open file is inside it; an
  unreadable aplexer or Hub listing skips the whole workspace pass;
- never a main checkout (only linked worktrees, ``.git`` is a file);
- never uncommitted or no-DONE work; unpushed commits are archived and kept;
- every removal is receipt-first (``disk-janitor-receipts.jsonl``): no durable
  receipt, no removal; git's refusal to remove a tree is never forced.

The pass reports a floor summary that the disk monitor attaches to the root
filesystem observation; the Hub alerts only when free space is still below the
floor after a completed cleanup pass (no informational alerts).

Stdlib only; git is called with argv arrays (no shell).
"""

from __future__ import annotations

import argparse
import hashlib
import glob
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .worktree_guard import parse_lock_reason

CONFIG_FILENAME = "disk-janitor.json"
STATE_FILENAME = "disk-janitor-state.json"
LOG_FILENAME = "disk-janitor.log"
RECEIPTS_FILENAME = "disk-janitor-receipts.jsonl"
# An 8-hex intake id token in a workspace name (agent-hub-wt-fcc31e56, ...-6b624e65-review).
INTAKE_TOKEN = re.compile(r"(?:^|[-_])[0-9a-f]{8}(?:[-_]|$)")
GIB = 1024 ** 3
MIB = 1024 ** 2
GIT_TIMEOUT = 120.0
LOG_MAX_BYTES = 2 * MIB

# Live aplexer phases; all other phases (including stopped and done) are terminal.
TERMINAL_PHASES = frozenset({"done", "stopped", "exited", "failed", "dead", "pruned", "forgotten"})

# Ignored entries that are regenerable build/test output, never evidence.
REGENERABLE_NAMES = frozenset({
    "node_modules", ".venv", "venv", "dist", "build", "target", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".wrangler", "coverage",
    ".next", ".turbo", ".cache", ".parcel-cache", ".tox", ".gradle",
    "playwright-report", "test-results", ".DS_Store", ".eggs", ".nyc_output",
    "tmp", "heavy-test", ".svelte-kit", ".astro",
})
REGENERABLE_SUFFIXES = (".pyc", ".pyo", ".egg-info", ".tsbuildinfo")

# Path components that mark data that is only ever moved, never deleted.
PROTECTED_COMPONENT = re.compile(r"(?i)^(prod|production)$|backup|dump|export")
SOURCE_SUFFIXES = (".py", ".pyi", ".pyc", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
                   ".go", ".rs", ".sh", ".bash", ".zsh", ".fish", ".yaml", ".yml")


def _protected_descendant_name(name: str) -> bool:
    """Keep backup/export artifacts inside otherwise-reapable cache trees."""
    if not PROTECTED_COMPONENT.search(name):
        return False
    # Source modules such as export.py are not exports. A backup-like source
    # filename still stays protected by the backup/dump component in its name.
    if name.lower().endswith(SOURCE_SUFFIXES) and not re.search(r"(?i)backup|dump", name):
        return False
    return True

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "dry_run": False,
    "mount": "/",
    "floor_gb": 100,
    "target_gb": 200,
    "interval_s": 900,
    "pressure_interval_s": 120,
    "workspace_roots": [],
    "grace_hours": 2.0,
    "orphan_idle_hours": 24.0,
    "archive_idle_hours": 72.0,
    "archive_dir": None,
    "archive_max_mb": 2048,
    "held_idle_hours": 24.0,
    "protected_paths": [],
    "cache_rules": [],
    "scratch_rules": [],
    "max_removals_per_run": 40,
    "time_budget_s": 900,
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _private_tree_manifest(root: str, members: Iterable[str], max_bytes: int) -> tuple[Dict[str, Dict[str, Any]], str, int]:
    """Hash selected ignored data without following symlinks or special files."""
    manifest: Dict[str, Dict[str, Any]] = {}
    total_bytes = 0
    pending = []
    for member in members:
        rel = os.path.normpath(member)
        if not rel or rel in (".", "..") or os.path.isabs(rel) or rel.startswith(".." + os.sep):
            raise ValueError("unsafe_private_member")
        pending.append((rel, os.path.join(root, rel)))
    while pending:
        rel, path = pending.pop()
        if rel in manifest:
            continue
        if len(manifest) >= 20000:
            raise ValueError("private_data_too_many_entries")
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            manifest[rel] = {"kind": "symlink", "target": os.readlink(path)}
        elif stat.S_ISDIR(st.st_mode):
            manifest[rel] = {"kind": "directory"}
            with os.scandir(path) as scan:
                children = sorted(scan, key=lambda item: item.name, reverse=True)
            for child in children:
                child_rel = os.path.join(rel, child.name)
                pending.append((child_rel, os.path.join(path, child.name)))
        elif stat.S_ISREG(st.st_mode):
            total_bytes += st.st_size
            if total_bytes > max_bytes:
                raise ValueError("private_data_too_large")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                    st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns
                ):
                    raise ValueError("private_data_changed")
                digest = hashlib.sha256()
                with os.fdopen(fd, "rb", closefd=False) as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                after = os.fstat(fd)
                if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                    opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns
                ):
                    raise ValueError("private_data_changed")
                manifest[rel] = {"kind": "file", "bytes": st.st_size, "sha256": digest.hexdigest()}
            finally:
                os.close(fd)
        else:
            raise ValueError("private_data_special_file")
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8", errors="surrogateescape")
    return manifest, hashlib.sha256(canonical).hexdigest(), total_bytes


class ConfigError(ValueError):
    pass


def _abs_list(raw: Any, key: str) -> List[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(p, str) or not os.path.isabs(p) or "\x00" in p for p in raw):
        raise ConfigError(f"{key} must be a list of absolute paths")
    return [os.path.normpath(p) for p in raw]


def _rules(raw: Any, key: str, path_key: str) -> List[Dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError(f"{key} must be a list")
    out = []
    for rule in raw:
        if not isinstance(rule, dict):
            raise ConfigError(f"{key} entries must be objects")
        target = rule.get(path_key)
        if not isinstance(target, str) or not os.path.isabs(target) or "\x00" in target:
            raise ConfigError(f"{key}.{path_key} must be an absolute path or glob")
        age = rule.get("min_age_hours")
        if isinstance(age, bool) or not isinstance(age, (int, float)) or not math.isfinite(age) or age < 1:
            raise ConfigError(f"{key}.min_age_hours must be >= 1")
        depth = rule.get("depth", 1)
        if not isinstance(depth, int) or not 1 <= depth <= 4:
            raise ConfigError(f"{key}.depth must be 1..4")
        out.append({path_key: os.path.normpath(target), "min_age_hours": float(age), "depth": depth})
    return out


def load_config(raw: Any) -> Optional[Dict[str, Any]]:
    """Validate a janitor config dict; None means disabled. Invalid raises."""
    if not isinstance(raw, dict) or raw.get("enabled") is False:
        return None
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError("unknown keys: " + ",".join(sorted(unknown)))
    cfg = dict(DEFAULTS)
    cfg.update(raw)
    for key in ("floor_gb", "target_gb"):
        if isinstance(cfg[key], bool) or not isinstance(cfg[key], (int, float)) or not math.isfinite(cfg[key]) or cfg[key] <= 0:
            raise ConfigError(f"{key} must be positive")
    if cfg["target_gb"] < cfg["floor_gb"]:
        raise ConfigError("target_gb must be >= floor_gb")
    for key in ("interval_s", "pressure_interval_s"):
        if not isinstance(cfg[key], int) or not 60 <= cfg[key] <= 86400:
            raise ConfigError(f"{key} must be 60..86400")
    for key in ("grace_hours", "orphan_idle_hours", "archive_idle_hours", "held_idle_hours"):
        if isinstance(cfg[key], bool) or not isinstance(cfg[key], (int, float)) or not math.isfinite(cfg[key]) or cfg[key] < 0.5:
            raise ConfigError(f"{key} must be >= 0.5")
    if not isinstance(cfg["mount"], str) or not os.path.isabs(cfg["mount"]):
        raise ConfigError("mount must be absolute")
    cfg["workspace_roots"] = _abs_list(cfg["workspace_roots"], "workspace_roots")
    cfg["protected_paths"] = _abs_list(cfg["protected_paths"], "protected_paths")
    if cfg["archive_dir"] is not None:
        if not isinstance(cfg["archive_dir"], str) or not os.path.isabs(cfg["archive_dir"]):
            raise ConfigError("archive_dir must be absolute")
        cfg["archive_dir"] = os.path.normpath(cfg["archive_dir"])
        # The archive itself is never a cleanup target.
        cfg["protected_paths"].append(cfg["archive_dir"])
    cfg["cache_rules"] = _rules(cfg["cache_rules"], "cache_rules", "path")
    cfg["scratch_rules"] = _rules(cfg["scratch_rules"], "scratch_rules", "glob")
    for key in ("max_removals_per_run", "time_budget_s", "archive_max_mb"):
        if isinstance(cfg[key], bool) or not isinstance(cfg[key], int) or cfg[key] < 1:
            raise ConfigError(f"{key} must be a positive integer")
    for key in ("dry_run",):
        if not isinstance(cfg[key], bool):
            raise ConfigError(f"{key} must be boolean")
    home = os.path.expanduser("~")
    # Built-in protection that config can extend but never remove.
    for builtin in (os.path.join(home, "prod"), os.path.join(home, "backups"), os.path.join(home, "storagebox"), home, "/"):
        if builtin not in cfg["protected_paths"]:
            cfg["protected_paths"].append(builtin)
    return cfg


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def protected_reason(path: str, cfg: Dict[str, Any]) -> Optional[str]:
    """Why ``path`` must never be deleted, or None. Ancestors of protected paths count."""
    if not isinstance(path, str) or not os.path.isabs(path):
        return "not_absolute"
    real = os.path.realpath(path)
    for candidate in {os.path.normpath(path), real}:
        if candidate == "/":
            return "protected_path"
        for prot in cfg["protected_paths"]:
            if _under(candidate, prot) and prot not in (os.path.expanduser("~"), "/"):
                return "protected_path"
            if prot != "/" and _under(prot, candidate):
                return "protected_ancestor"
        for part in candidate.split(os.sep):
            if part and PROTECTED_COMPONENT.search(part):
                return "protected_name"
    return None


def _newest_mtime(path: str, *, max_entries: int = 20000, expired: Callable[[], bool] = lambda: False,
                  scan_protected: bool = False) -> Optional[float]:
    """Newest mtime under ``path`` (bounded walk). None means unknown, or (with
    ``scan_protected``) a protected-named descendant such as a backup: keep it."""
    newest = 0.0
    seen = 0
    stack = [path]
    while stack:
        current = stack.pop()
        if scan_protected and current != path and _protected_descendant_name(os.path.basename(current)):
            return None
        try:
            st = os.lstat(current)
        except OSError:
            return None
        newest = max(newest, st.st_mtime)
        seen += 1
        if seen > max_entries or expired():
            return None
        if os.path.isdir(current) and not os.path.islink(current):
            try:
                stack.extend(os.path.join(current, name) for name in os.listdir(current))
            except OSError:
                return None
    return newest


def tree_bytes(path: str, *, max_entries: int = 200000) -> int:
    total = 0
    seen = 0
    stack = [path]
    while stack and seen < max_entries:
        current = stack.pop()
        try:
            st = os.lstat(current)
        except OSError:
            continue
        seen += 1
        total += getattr(st, "st_blocks", 0) * 512
        if os.path.isdir(current) and not os.path.islink(current):
            try:
                stack.extend(os.path.join(current, name) for name in os.listdir(current))
            except OSError:
                pass
    return total


def proc_open_paths(proc_root: str = "/proc") -> Optional[List[str]]:
    """Every cwd and open-file target of visible processes. None when /proc is unreadable."""
    paths: List[str] = []
    try:
        pids = [p for p in os.listdir(proc_root) if p.isdigit()]
    except OSError:
        return None
    for pid in pids:
        base = os.path.join(proc_root, pid)
        try:
            target = os.readlink(os.path.join(base, "cwd"))
            paths.append(target[: -len(" (deleted)")] if target.endswith(" (deleted)") else target)
        except OSError:
            pass
        try:
            fds = os.listdir(os.path.join(base, "fd"))
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(base, "fd", fd))
            except OSError:
                continue
            if target.startswith("/"):
                paths.append(target)
    return paths


def ancestor_set(paths: Iterable[str]) -> frozenset:
    """Every open path plus all its ancestors, so "is anything open inside X" is one lookup."""
    out = set()
    for raw in paths:
        if not isinstance(raw, str) or not raw.startswith("/"):
            continue
        current = os.path.normpath(raw)
        while current not in out:
            out.add(current)
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    return frozenset(out)


def aplexer_sessions(aplexer_bin: str = "aplexer") -> Optional[List[Dict[str, Any]]]:
    try:
        proc = subprocess.run([aplexer_bin, "snapshot"], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    if isinstance(data, dict):
        data = data.get("sessions")
    return data if isinstance(data, list) else None


def _git(argv: List[str], cwd: str, timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    # --no-optional-locks: `git status` must not refresh the index, or every
    # pass would reset the idle clock that activity_time() reads.
    return subprocess.run(["git", "--no-optional-locks", *argv], cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, check=False)


def worktree_gitdir(path: str) -> Optional[str]:
    """The private gitdir of a LINKED worktree; None for main checkouts or non-git dirs."""
    dotgit = os.path.join(path, ".git")
    if not os.path.isfile(dotgit) or os.path.islink(dotgit):
        return None
    try:
        with open(dotgit, "r", encoding="utf-8") as handle:
            line = handle.read(4096).strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = line[len("gitdir:"):].strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.normpath(os.path.join(path, gitdir))
    if os.sep + "worktrees" + os.sep not in gitdir or not os.path.isdir(gitdir):
        return None
    return gitdir


def hub_lock_session(gitdir: str) -> Any:
    """None when unlocked; the session id of a Hub lifecycle lock; False for any other lock."""
    lock_file = os.path.join(gitdir, "locked")
    if not os.path.exists(lock_file):
        return None
    try:
        with open(lock_file, "r", encoding="utf-8") as handle:
            text = handle.read(4096).strip()
    except OSError:
        return False
    parsed = parse_lock_reason(text)
    return parsed[0] if parsed else False


def discover_worktrees(roots: Iterable[str]) -> List[str]:
    found = []
    expanded = sorted({os.path.normpath(r) for root in roots for r in (glob.glob(root) if glob.has_magic(root) else [root])})
    for root in expanded:
        try:
            entries = sorted(os.scandir(root), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False) and worktree_gitdir(entry.path):
                found.append(os.path.normpath(entry.path))
    return found


def activity_time(path: str, gitdir: str) -> float:
    """Last observable activity: tree top level, git HEAD/index/reflog of the worktree."""
    newest = 0.0
    candidates = [path, os.path.join(path, ".git")]
    candidates += [os.path.join(gitdir, n) for n in ("HEAD", "index", "ORIG_HEAD", "FETCH_HEAD", os.path.join("logs", "HEAD"))]
    try:
        candidates += [os.path.join(path, n) for n in os.listdir(path)]
    except OSError:
        pass
    for candidate in candidates:
        try:
            st = os.lstat(candidate)
        except OSError:
            continue
        newest = max(newest, st.st_mtime)
    return newest


def _ignored_private(path: str, entries: List[str]) -> List[str]:
    """Ignored entries that are not regenerable output (e.g. ``.local`` evidence)."""
    private: List[str] = []
    for rel in entries:
        clean = rel.rstrip("/")
        base = os.path.basename(clean)
        if base in REGENERABLE_NAMES or base.endswith(REGENERABLE_SUFFIXES):
            continue
        full = os.path.join(path, clean)
        if rel.endswith("/") and os.path.isdir(full) and not os.path.islink(full):
            # Descend one level so `.local/tmp` scratch does not count as evidence.
            try:
                children = sorted(os.listdir(full))
            except OSError:
                private.append(clean)
                continue
            for child in children:
                if child in REGENERABLE_NAMES or child.endswith(REGENERABLE_SUFFIXES):
                    continue
                private.append(os.path.join(clean, child))
            continue
        private.append(clean)
    return private


def git_facts(path: str) -> Dict[str, Any]:
    """Dirty/unpushed/ignored facts for one worktree. Raises on any git failure."""
    status = _git(["status", "--porcelain=v1", "-z", "--ignored=traditional", "--untracked-files=normal"], path)
    if status.returncode != 0:
        raise RuntimeError("git_status_failed")
    dirty: List[str] = []
    ignored: List[str] = []
    for item in status.stdout.split("\0"):
        if not item:
            continue
        code, rel = item[:2], item[3:]
        if code == "!!":
            ignored.append(rel)
        elif code[0] in "RC":
            dirty.append(rel)
        else:
            dirty.append(rel)
    head = _git(["rev-parse", "--verify", "-q", "HEAD"], path)
    if head.returncode != 0:
        raise RuntimeError("git_head_failed")
    unpushed = _git(["rev-list", "--max-count=1", "HEAD", "--not", "--remotes"], path)
    if unpushed.returncode != 0:
        raise RuntimeError("git_rev_list_failed")
    remotes = _git(["for-each-ref", "--count=1", "--format=%(refname)", "refs/remotes"], path)
    branch = _git(["symbolic-ref", "-q", "--short", "HEAD"], path)
    base = None
    for ref in ("refs/remotes/origin/HEAD", "refs/remotes/origin/main", "refs/remotes/origin/master"):
        resolved = _git(["rev-parse", "--verify", "-q", ref + "^{commit}"], path)
        if resolved.returncode == 0 and resolved.stdout.strip():
            base = resolved.stdout.strip()
            break
    merged = False
    if base:
        ancestor = _git(["merge-base", "--is-ancestor", "HEAD", base], path)
        if ancestor.returncode not in (0, 1):
            raise RuntimeError("git_merge_base_failed")
        merged = ancestor.returncode == 0
    return {
        "head": head.stdout.strip(),
        "branch": branch.stdout.strip() if branch.returncode == 0 else None,
        "base": base,
        "dirty": dirty,
        "unpushed": bool(unpushed.stdout.strip()) or not remotes.stdout.strip(),
        "merged": merged,
        "ignored_private": _ignored_private(path, ignored),
    }


class DiskJanitor:
    """One host's disk floor janitor. ``maybe_start`` is single-inflight and rate-limited."""

    def __init__(
        self,
        config_path: Optional[str] = None,
        *,
        config: Optional[Dict[str, Any]] = None,
        state_dir: Optional[str] = None,
        hub_request: Optional[Callable[[str, str], Any]] = None,
        sessions_fn: Optional[Callable[[], Optional[List[Dict[str, Any]]]]] = None,
        open_paths_fn: Optional[Callable[[], Optional[List[str]]]] = None,
        free_fn: Optional[Callable[[str], int]] = None,
        time_fn: Callable[[], float] = time.time,
        monotonic_fn: Callable[[], float] = time.monotonic,
        ismount_fn: Callable[[str], bool] = os.path.ismount,
        dev_fn: Callable[[str], int] = lambda p: os.stat(p).st_dev,
    ):
        self.config_path = config_path
        self._config_override = config
        self.state_dir = state_dir or (os.path.dirname(config_path) if config_path else None)
        self.hub_request = hub_request
        self.sessions_fn = sessions_fn or aplexer_sessions
        self.open_paths_fn = open_paths_fn or proc_open_paths
        self.free_fn = free_fn or (lambda mount: shutil.disk_usage(mount).free)
        self.time_fn = time_fn
        self.monotonic_fn = monotonic_fn
        self.ismount_fn = ismount_fn
        self.dev_fn = dev_fn
        self._thread: Optional[threading.Thread] = None
        self._last_start = -1e18
        self._lock = threading.Lock()
        self.last_result: Optional[Dict[str, Any]] = self._load_state()

    # ----- configuration and state -------------------------------------------------
    def active_config(self) -> Optional[Dict[str, Any]]:
        if self._config_override is not None:
            return load_config(self._config_override)
        if not self.config_path or not os.path.isfile(self.config_path):
            return None
        with open(self.config_path, "r", encoding="utf-8") as handle:
            return load_config(json.load(handle))

    def _load_state(self) -> Optional[Dict[str, Any]]:
        if not self.state_dir:
            return None
        try:
            with open(os.path.join(self.state_dir, STATE_FILENAME), "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _save_state(self, result: Dict[str, Any]) -> None:
        if not self.state_dir:
            return
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            target = os.path.join(self.state_dir, STATE_FILENAME)
            tmp = target + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(result, handle, indent=1, sort_keys=True)
            os.replace(tmp, target)
            log = os.path.join(self.state_dir, LOG_FILENAME)
            if os.path.exists(log) and os.path.getsize(log) > LOG_MAX_BYTES:
                os.replace(log, log + ".1")
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(result, sort_keys=True) + "\n")
        except OSError:
            pass

    def _receipt(self, item: Dict[str, Any]) -> bool:
        """Append-only, never rotated: one fsynced line per cleanup/archive effect."""
        if not self.state_dir:
            return False
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            with open(os.path.join(self.state_dir, RECEIPTS_FILENAME), "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"at": utc_now_iso(), **item}, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return True
        except OSError:
            return False

    def _recheck(self, path: str, decision: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Fresh process, session, Hub, and git proof just before a removal."""
        fresh = self.open_paths_fn()
        if fresh is None or self._in_use(path, ancestor_set(fresh)):
            return "process_inside"
        sessions = self.sessions_fn()
        if sessions is None:
            return "aplexer_unreadable"
        facts = self._hub_facts()
        if facts is None:
            return "hub_unreadable"
        refs = [s for s in sessions if isinstance(s, dict) and any(
            isinstance(s.get(key), str) and _under(os.path.normpath(s[key]), path)
            for key in ("workspace", "cwd")
        )]
        if any(s.get("phase") not in TERMINAL_PHASES for s in refs):
            return "live_session"
        gitdir = worktree_gitdir(path)
        lock_session = hub_lock_session(gitdir) if gitdir else None
        hub = [f for f in facts if isinstance(f, dict) and (
            (isinstance(f.get("workspace"), str) and _under(os.path.normpath(f["workspace"]), path))
            or (lock_session and f.get("session_id") == lock_session)
        )]
        if any(f.get("terminal") is not True or f.get("native_terminal") is not True for f in hub):
            return "live_hub_session"
        if any(f.get("linked_intake_open") is not False for f in hub):
            return "intake_not_done"
        if not refs and not hub and INTAKE_TOKEN.search(os.path.basename(path)):
            return "intake_unknown"
        try:
            now = git_facts(path)
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            return "git_unreadable"
        if decision.get("unpushed_archive"):
            # This path only adds a verified backup; it never removes the source.
            return None if now["head"] == decision.get("head") else "head_moved"
        if now["dirty"]:
            return "uncommitted_changes"
        if now["unpushed"]:
            return "unpushed_commits"
        if now["head"] != decision.get("head"):
            return "head_moved"
        if decision.get("private_manifest_sha256"):
            expected = sorted(decision.get("ignored_private", []))
            if sorted(now.get("ignored_private", [])) != expected:
                return "private_data_changed"
            try:
                _, digest, _ = _private_tree_manifest(
                    path, [item.rstrip("/") for item in expected],
                    (cfg or {}).get("archive_max_mb", DEFAULTS["archive_max_mb"]) * MIB,
                )
            except (OSError, ValueError, UnicodeError):
                return "private_data_changed"
            if digest != decision["private_manifest_sha256"]:
                return "private_data_changed"
        return None

    # ----- scheduling ---------------------------------------------------------------
    def floor_report(self, mount: str) -> Optional[Dict[str, Any]]:
        """Compact floor summary for the disk observation of ``mount`` (or None)."""
        result = self.last_result
        if not isinstance(result, dict) or result.get("mount") != mount or not result.get("completed_at"):
            try:
                cfg = self.active_config()
            except (OSError, ValueError):
                return None
            if not cfg or cfg["mount"] != mount:
                return None
            return {"floor_bytes": int(cfg["floor_gb"] * GIB), "target_bytes": int(cfg["target_gb"] * GIB),
                    "cleanup_at": None, "free_after_cleanup_bytes": None}
        return {
            "floor_bytes": result["floor_bytes"],
            "target_bytes": result["target_bytes"],
            "cleanup_at": result["completed_at"],
            "free_after_cleanup_bytes": result["free_after_bytes"],
            "freed_bytes": result.get("freed_bytes", 0),
            "removed": len(result.get("removed", [])),
            "dry_run": bool(result.get("dry_run")),
        }

    def due(self, cfg: Dict[str, Any]) -> bool:
        try:
            below = self.free_fn(cfg["mount"]) < cfg["floor_gb"] * GIB
        except OSError:
            below = False
        interval = cfg["pressure_interval_s"] if below else cfg["interval_s"]
        return self.monotonic_fn() - self._last_start >= interval

    def maybe_start(self) -> bool:
        """Start one background pass when due; never raises, never blocks."""
        try:
            with self._lock:
                if self._thread is not None and self._thread.is_alive():
                    return False
                cfg = self.active_config()
                if not cfg or not self.due(cfg):
                    return False
                self._last_start = self.monotonic_fn()
                self._thread = threading.Thread(target=self._run_safely, args=(cfg,), daemon=True, name="disk-janitor")
                self._thread.start()
                return True
        except Exception as error:  # config errors must not stop disk monitoring
            print(json.dumps({"event": "disk_janitor_error", "type": type(error).__name__}), flush=True)
            return False

    def _run_safely(self, cfg: Dict[str, Any]) -> None:
        try:
            result = self.run_once(cfg)
            print(json.dumps({"event": "disk_janitor_cycle", "freed_bytes": result.get("freed_bytes"),
                              "removed": len(result.get("removed", [])), "free_after_bytes": result.get("free_after_bytes"),
                              "errors": len(result.get("errors", []))}), flush=True)
        except Exception as error:
            print(json.dumps({"event": "disk_janitor_error", "type": type(error).__name__}), flush=True)

    # ----- the pass -----------------------------------------------------------------
    def run_once(self, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        cfg = cfg or self.active_config()
        if not cfg:
            return {"disabled": True}
        deadline = self.monotonic_fn() + cfg["time_budget_s"]
        mount = cfg["mount"]
        free_before = self.free_fn(mount)
        target = int(cfg["target_gb"] * GIB)
        result: Dict[str, Any] = {
            "mount": mount, "started_at": utc_now_iso(), "dry_run": cfg["dry_run"],
            "floor_bytes": int(cfg["floor_gb"] * GIB), "target_bytes": target,
            "free_before_bytes": free_before, "removed": [], "archived": [], "kept": {}, "errors": [],
        }
        raw_open = self.open_paths_fn()
        if raw_open is None:
            result["errors"].append("proc_unreadable")
        else:
            open_paths = ancestor_set(raw_open)
            self._workspace_pass(cfg, result, open_paths, deadline)
            self._age_pass(cfg, result, open_paths, deadline, "cache_rules", "path")
            self._age_pass(cfg, result, open_paths, deadline, "scratch_rules", "glob")
        free_after = self.free_fn(mount)
        result.update({"completed_at": utc_now_iso(), "free_after_bytes": free_after,
                       "freed_bytes": max(0, free_after - free_before),
                       "below_floor_after_cleanup": free_after < result["floor_bytes"]})
        self.last_result = result
        self._save_state(result)
        return result

    def _keep(self, result: Dict[str, Any], path: str, reason: str) -> None:
        result["kept"].setdefault(reason, []).append(path)

    def _budget_left(self, cfg: Dict[str, Any], result: Dict[str, Any], deadline: float) -> bool:
        return self.monotonic_fn() < deadline and len(result["removed"]) < cfg["max_removals_per_run"]

    def _under_pressure(self, cfg: Dict[str, Any]) -> bool:
        try:
            return self.free_fn(cfg["mount"]) < cfg["target_gb"] * GIB
        except OSError:
            return False

    @staticmethod
    def _in_use(path: str, open_paths: Any) -> bool:
        """True when any open path is ``path`` or inside it (``open_paths`` from ancestor_set)."""
        if isinstance(open_paths, (set, frozenset)):
            return os.path.normpath(path) in open_paths
        return any(_under(p, path) for p in open_paths)

    def _hub_facts(self) -> Optional[List[Dict[str, Any]]]:
        if self.hub_request is None:
            return None
        try:
            data = self.hub_request("GET", "/api/sessions/worktree-guards")
        except Exception:
            return None
        sessions = data.get("sessions") if isinstance(data, dict) else None
        return sessions if isinstance(sessions, list) else None

    def classify_worktree(self, cfg: Dict[str, Any], path: str, sessions: List[Dict[str, Any]],
                          facts: List[Dict[str, Any]], open_paths: Any) -> Dict[str, Any]:
        """Decide remove | archive | keep for one linked worktree. Pure except for git reads.

        A tree with uncommitted changes or a linked intake without DONE is never
        removed. Clean unpushed commits are bundled to the network disk and kept
        in place. A clean pushed branch may be removed before it is merged to
        origin/main. ``archive`` also covers non-regenerable ignored data.
        """
        decision = {"path": path, "action": "keep"}
        reason = protected_reason(path, cfg)
        if reason:
            return {**decision, "reason": reason}
        gitdir = worktree_gitdir(path)
        if not gitdir:
            return {**decision, "reason": "not_linked_worktree"}
        lock_session = hub_lock_session(gitdir)
        if lock_session is False:
            return {**decision, "reason": "git_locked"}
        if self._in_use(path, open_paths):
            return {**decision, "reason": "process_inside"}
        refs = [s for s in sessions if isinstance(s, dict) and any(
            isinstance(s.get(k), str) and _under(os.path.normpath(s[k]), path) for k in ("workspace", "cwd"))]
        if any(s.get("phase") not in TERMINAL_PHASES for s in refs):
            return {**decision, "reason": "live_session"}
        hub = [f for f in facts if isinstance(f, dict) and (
            (isinstance(f.get("workspace"), str) and _under(os.path.normpath(f["workspace"]), path))
            or (lock_session and f.get("session_id") == lock_session))]
        if lock_session and not any(f.get("session_id") == lock_session for f in hub):
            return {**decision, "reason": "git_locked_unknown_session"}
        if any(f.get("terminal") is not True or f.get("native_terminal") is not True for f in hub):
            return {**decision, "reason": "live_hub_session"}
        # A linked intake must be DONE/archived (Hub's linked_intake_open is False);
        # open or unknown keeps the tree.
        if any(f.get("linked_intake_open") is not False for f in hub):
            return {**decision, "reason": "intake_not_done"}
        orphan = not refs and not hub
        if not hub and INTAKE_TOKEN.search(os.path.basename(path)):
            # Named for an intake the Hub does not link (an aplexer session alone
            # says nothing about DONE): its DONE state is unknown.
            return {**decision, "reason": "intake_unknown"}
        # Held: a human-protected session or a Hub lifecycle lock (intake DONE).
        held = bool(lock_session) or any(f.get("protected") is not False for f in hub)
        now = self.time_fn()
        last = activity_time(path, gitdir)
        for s in refs:
            for key in ("last_activity_ms", "updated_at_ms"):
                if isinstance(s.get(key), (int, float)):
                    last = max(last, s[key] / 1000.0)
            exit_info = s.get("exit") if isinstance(s.get("exit"), dict) else {}
            if isinstance(exit_info.get("exited_at_ms"), (int, float)):
                last = max(last, exit_info["exited_at_ms"] / 1000.0)
        idle_h = (now - last) / 3600.0
        decision["idle_hours"] = round(idle_h, 1)
        grace = cfg["orphan_idle_hours"] if orphan else cfg["held_idle_hours"] if held else cfg["grace_hours"]
        if idle_h < grace:
            return {**decision, "reason": "held_grace_period" if held else "grace_period"}
        try:
            facts_git = git_facts(path)
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            return {**decision, "reason": "git_unreadable"}
        decision.update({"branch": facts_git["branch"], "head": facts_git["head"], "base": facts_git["base"]})
        kind = "held" if held else "orphan" if orphan else "terminal"
        decision["unlock"] = bool(lock_session)
        if facts_git["unpushed"]:
            decision.update({"action": "archive", "unpushed_archive": True,
                             "uncommitted_changes": bool(facts_git["dirty"]),
                             "reason": kind + "_unpushed_commits"})
            return decision
        if facts_git["dirty"]:
            return {**decision, "reason": "uncommitted_changes"}
        pushed_kind = "merged" if facts_git["merged"] else "pushed"
        if facts_git["ignored_private"]:
            for rel in facts_git["ignored_private"]:
                if protected_reason(os.path.join(path, rel.rstrip("/")), {**cfg, "protected_paths": []}):
                    return {**decision, "reason": "holds_protected_data"}
            if idle_h < cfg["archive_idle_hours"]:
                return {**decision, "reason": "private_data_recent"}
            decision["ignored_private"] = facts_git["ignored_private"]
            return {**decision, "action": "archive", "reason": kind + "_" + pushed_kind + "_private_data"}
        return {**decision, "action": "remove", "reason": kind + "_clean_" + pushed_kind}

    def _workspace_pass(self, cfg, result, open_paths, deadline) -> None:
        if not cfg["workspace_roots"]:
            return
        sessions = self.sessions_fn()
        facts = self._hub_facts()
        if sessions is None or facts is None:
            result["errors"].append("aplexer_unreadable" if sessions is None else "hub_unreadable")
            return
        for path in discover_worktrees(cfg["workspace_roots"]):
            if not self._budget_left(cfg, result, deadline):
                result["errors"].append("budget_exhausted")
                return
            decision = self.classify_worktree(cfg, path, sessions, facts, open_paths)
            if decision["action"] == "keep":
                self._keep(result, path, decision["reason"])
                continue
            if decision.get("unpushed_archive"):
                if cfg["dry_run"]:
                    result["archived"].append({**decision, "dry_run": True})
                    self._keep(result, path, "unpushed_archive_dry_run")
                    continue
                archive = self.archive_unpushed_worktree(cfg, path, decision)
                if not archive.get("ok"):
                    self._keep(result, path, "archive_" + archive.get("error", "failed"))
                    continue
                stale = self._recheck(path, decision, cfg)
                if stale:
                    self._keep(result, path, stale)
                    continue
                result["archived"].append(archive)
                receipt = {**decision, "archive": archive["archive"], "bytes": archive["bytes"],
                           "outcome": "unpushed_archived_kept"}
                if not self._receipt(receipt):
                    result["errors"].append("archive_receipt_unwritable")
                    self._keep(result, path, "unpushed_archive_receipt_unwritable")
                else:
                    self._keep(result, path, "unpushed_archive_kept")
                continue
            kind = decision["reason"].split("_")[0]
            if (decision["action"] == "archive" or kind in ("orphan", "held")) and not self._under_pressure(cfg):
                self._keep(result, path, kind + "_no_pressure" if decision["action"] == "remove" else "private_data_no_pressure")
                continue
            size = tree_bytes(path)
            if cfg["dry_run"]:
                result["removed"].append({**decision, "bytes": size, "dry_run": True})
                continue
            archive = None
            if decision["action"] == "archive":
                archive = self.archive_worktree(cfg, path, decision)
                if not archive.get("ok"):
                    self._keep(result, path, "archive_" + archive.get("error", "failed"))
                    continue
                decision = {**decision, "private_manifest_sha256": archive["private_manifest_sha256"]}
                result["archived"].append(archive)
            # Re-check right before the effect (after any archive step): a session
            # may have started or someone may have written into the tree.
            stale = self._recheck(path, decision, cfg)
            if stale:
                self._keep(result, path, stale)
                continue
            item = {**decision, "bytes": size}
            item.pop("ignored_private", None)
            if archive:
                item["archive"] = archive["archive"]
            # Receipt first: no durable receipt, no removal.
            if not self._receipt({**item, "outcome": "removing"}):
                self._keep(result, path, "receipt_unwritable")
                continue
            if self.remove_worktree(path, decision.get("branch"), unlock=bool(decision.get("unlock"))):
                result["removed"].append(item)
                self._receipt({**item, "outcome": "removed"})
            else:
                self._receipt({**item, "outcome": "kept_remove_refused"})
                self._keep(result, path, "remove_failed")

    def archive_worktree(self, cfg: Dict[str, Any], path: str, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Tar the non-regenerable ignored data of a clean, pushed tree to network disk.

        Only called for trees already proven clean and pushed, so there is no
        unpushed history or tracked change to carry. The tar is re-read and every
        member checked; the archive is kept. Any doubt returns ok=False.
        """
        archive_dir = cfg.get("archive_dir")
        if not archive_dir:
            return {"ok": False, "error": "no_archive_dir"}
        mount_root = archive_dir
        while mount_root != os.path.dirname(mount_root) and not self.ismount_fn(mount_root):
            mount_root = os.path.dirname(mount_root)
        if mount_root == os.path.dirname(mount_root):
            return {"ok": False, "error": "archive_not_mounted"}
        if self.dev_fn(mount_root) == self.dev_fn(cfg["mount"]):
            return {"ok": False, "error": "archive_same_filesystem"}
        members = [m.rstrip("/") for m in decision.get("ignored_private", [])]
        if not members:
            return {"ok": False, "error": "nothing_to_archive"}
        # Do not add overlapping paths twice: each selected top-level member
        # already includes all descendants in the archive.
        members = sorted(set(members), key=lambda item: (item.count(os.sep), item))
        members = [member for member in members if not any(_under(member, parent) for parent in members if parent != member)]
        payload = sum(tree_bytes(os.path.join(path, m)) for m in members)
        if payload > cfg["archive_max_mb"] * MIB:
            return {"ok": False, "error": "too_large"}
        try:
            before, source_digest, logical_bytes = _private_tree_manifest(
                path, members, cfg["archive_max_mb"] * MIB,
            )
        except (OSError, ValueError, UnicodeError):
            return {"ok": False, "error": "private_data_unreadable"}
        if logical_bytes > cfg["archive_max_mb"] * MIB:
            return {"ok": False, "error": "too_large"}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = os.path.join(archive_dir, f"{os.path.basename(path)}-{stamp}")
        try:
            os.makedirs(dest, mode=0o700)
            tar_path = os.path.join(dest, "files.tar.gz")
            expected = set(before)
            with tarfile.open(tar_path, "w:gz") as tar:
                for member in members:
                    tar.add(os.path.join(path, member), arcname=member)
            with tarfile.open(tar_path, "r:gz") as tar:
                infos = tar.getmembers()
                names = [info.name.rstrip("/") for info in infos]
                if len(names) != len(set(names)):
                    return {"ok": False, "error": "tar_verify_failed"}
                actual = {}
                for info in tar.getmembers():
                    name = info.name.rstrip("/")
                    if not name or os.path.isabs(name) or name == ".." or name.startswith(".." + os.sep):
                        return {"ok": False, "error": "tar_verify_failed"}
                    if info.isdir():
                        actual[name] = {"kind": "directory"}
                    elif info.issym():
                        actual[name] = {"kind": "symlink", "target": info.linkname}
                    elif info.isfile():
                        handle = tar.extractfile(info)
                        if handle is None:
                            return {"ok": False, "error": "tar_verify_failed"}
                        digest = hashlib.sha256()
                        size = 0
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            size += len(chunk)
                            digest.update(chunk)
                        actual[name] = {"kind": "file", "bytes": size, "sha256": digest.hexdigest()}
                    else:
                        return {"ok": False, "error": "tar_verify_failed"}
            if actual != before:
                return {"ok": False, "error": "tar_verify_failed"}
            _, after_digest, _ = _private_tree_manifest(path, members, cfg["archive_max_mb"] * MIB)
            if after_digest != source_digest:
                return {"ok": False, "error": "private_data_changed"}
            meta = {"path": path, "head": decision.get("head"), "base": decision.get("base"),
                    "branch": decision.get("branch"), "archived_at": utc_now_iso(),
                    "reason": decision.get("reason"), "members": members[:200], "entries": len(names)}
            with open(os.path.join(dest, "meta.json"), "w", encoding="utf-8") as handle:
                json.dump(meta, handle, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
        except (OSError, tarfile.TarError, EOFError, UnicodeError):
            return {"ok": False, "error": "write_failed"}
        return {"ok": True, "path": path, "archive": dest, "bytes": payload,
                "private_manifest_sha256": source_digest}

    def archive_unpushed_worktree(self, cfg: Dict[str, Any], path: str,
                                  decision: Dict[str, Any]) -> Dict[str, Any]:
        """Create and verify an idempotent git bundle on the network disk.

        The source worktree is deliberately retained. The bundle is an extra
        copy of unpushed history, not permission to remove its worktree.
        """
        archive_dir = cfg.get("archive_dir")
        if not archive_dir:
            return {"ok": False, "error": "no_archive_dir"}
        mount_root = archive_dir
        while mount_root != os.path.dirname(mount_root) and not self.ismount_fn(mount_root):
            mount_root = os.path.dirname(mount_root)
        if mount_root == os.path.dirname(mount_root):
            return {"ok": False, "error": "archive_not_mounted"}
        try:
            if self.dev_fn(mount_root) == self.dev_fn(cfg["mount"]):
                return {"ok": False, "error": "archive_same_filesystem"}
        except OSError:
            return {"ok": False, "error": "archive_mount_unreadable"}

        head = str(decision.get("head") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
            return {"ok": False, "error": "invalid_head"}
        identity = hashlib.sha256(os.path.realpath(path).encode("utf-8", errors="surrogateescape")).hexdigest()[:12]
        name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(path))[:48] or "worktree"
        dest = os.path.join(archive_dir, f"unpushed-{name}-{identity}-{head[:12]}")
        bundle_path = os.path.join(dest, "commits.bundle")

        def existing_is_valid() -> bool:
            verified = _git(["bundle", "verify", bundle_path], path, timeout=GIT_TIMEOUT)
            listed = _git(["bundle", "list-heads", bundle_path], path, timeout=GIT_TIMEOUT)
            return verified.returncode == 0 and listed.returncode == 0 and any(
                line.split() and line.split()[0].lower() == head.lower()
                for line in listed.stdout.splitlines()
            )

        if os.path.isdir(dest):
            if existing_is_valid():
                return {"ok": True, "path": path, "archive": dest,
                        "bytes": os.path.getsize(bundle_path), "reused": True}
            return {"ok": False, "error": "existing_archive_invalid"}

        try:
            os.makedirs(archive_dir, mode=0o700, exist_ok=True)
            temp_dir = tempfile.mkdtemp(prefix=".disk-janitor-bundle-", dir=archive_dir)
        except OSError:
            return {"ok": False, "error": "archive_unwritable"}
        try:
            temp_bundle = os.path.join(temp_dir, "commits.bundle")
            argv = ["bundle", "create", temp_bundle, "HEAD"]
            base = decision.get("base")
            if isinstance(base, str) and re.fullmatch(r"[0-9a-fA-F]{40,64}", base):
                argv.append("^" + base)
            created = _git(argv, path, timeout=GIT_TIMEOUT)
            if created.returncode != 0:
                return {"ok": False, "error": "bundle_create_failed"}
            size = os.path.getsize(temp_bundle)
            if size > cfg["archive_max_mb"] * MIB:
                return {"ok": False, "error": "too_large"}
            verified = _git(["bundle", "verify", temp_bundle], path, timeout=GIT_TIMEOUT)
            listed = _git(["bundle", "list-heads", temp_bundle], path, timeout=GIT_TIMEOUT)
            if verified.returncode != 0 or listed.returncode != 0 or not any(
                line.split() and line.split()[0].lower() == head.lower()
                for line in listed.stdout.splitlines()
            ):
                return {"ok": False, "error": "bundle_verify_failed"}
            digest = _sha256_file(temp_bundle)
            with open(temp_bundle, "rb") as handle:
                os.fsync(handle.fileno())
            meta = {"path": path, "branch": decision.get("branch"), "head": head,
                    "base": base, "archived_at": utc_now_iso(), "sha256": digest,
                    "bytes": size, "reason": decision.get("reason")}
            with open(os.path.join(temp_dir, "meta.json"), "w", encoding="utf-8") as handle:
                json.dump(meta, handle, indent=1, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                directory_fd = os.open(temp_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # Some network filesystems do not implement directory fsync.
                pass
            os.rename(temp_dir, dest)
            return {"ok": True, "path": path, "archive": dest, "bytes": size,
                    "sha256": digest, "reused": False}
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            if os.path.isdir(dest) and existing_is_valid():
                return {"ok": True, "path": path, "archive": dest,
                        "bytes": os.path.getsize(bundle_path), "reused": True}
            return {"ok": False, "error": "write_failed"}
        finally:
            if os.path.isdir(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def remove_worktree(self, path: str, branch: Optional[str], *, unlock: bool = False) -> bool:
        """``git worktree remove`` without --force: git's own refusal on modified
        or untracked files is the last line of defence and is never overridden."""
        gitdir = worktree_gitdir(path)
        if not gitdir:
            return False
        common = os.path.dirname(os.path.dirname(gitdir))
        lock_file = os.path.join(gitdir, "locked")
        lock_reason_text = None
        if os.path.exists(lock_file):
            # Only a Hub lifecycle lock of a proven-terminal session is released;
            # it is restored if the removal does not happen.
            if not unlock or not hub_lock_session(gitdir):
                return False
            with open(lock_file, "r", encoding="utf-8") as handle:
                lock_reason_text = handle.read().strip()
            if _git(["worktree", "unlock", path], common).returncode != 0:
                return False
        done = _git(["worktree", "remove", path], common, timeout=900)
        if done.returncode != 0:
            if lock_reason_text is not None and os.path.isdir(path):
                _git(["worktree", "lock", "--reason", lock_reason_text, path], common)
            return False
        if branch:
            # Only a fully merged branch is deleted (-d); unmerged branches stay.
            _git(["branch", "-d", branch], common)
        return not os.path.exists(path)

    def _age_pass(self, cfg, result, open_paths, deadline, key: str, path_key: str) -> None:
        now = self.time_fn()
        for rule in cfg[key]:
            # A scratch glob matches the removable entries themselves; a cache
            # path names a directory whose entries at ``depth`` are removable.
            depth = 0 if path_key == "glob" else rule["depth"]
            for root in sorted(glob.glob(rule[path_key])):
                candidates = [root]
                for _ in range(depth):
                    nxt = []
                    for c in candidates:
                        if os.path.isdir(c) and not os.path.islink(c):
                            try:
                                nxt += [os.path.join(c, n) for n in sorted(os.listdir(c))]
                            except OSError:
                                pass
                    candidates = nxt
                for entry in candidates:
                    if not self._budget_left(cfg, result, deadline):
                        result["errors"].append("budget_exhausted")
                        return
                    if path_key == "glob" and (not os.path.isdir(entry) or os.path.islink(entry)):
                        # Scratch rules are for disposable test-home directories,
                        # not standalone logs or symlinks that happen to match.
                        self._keep(result, entry, "scratch_not_directory")
                        continue
                    reason = protected_reason(entry, cfg)
                    if reason:
                        self._keep(result, entry, reason)
                        continue
                    if any(_under(r, entry) for r in cfg["workspace_roots"]):
                        self._keep(result, entry, "workspace_root")
                        continue
                    if os.path.lexists(os.path.join(entry, ".git")):
                        self._keep(result, entry, "git_checkout")
                        continue
                    if os.path.islink(entry):
                        self._keep(result, entry, "symlink")
                        continue
                    if self._in_use(entry, open_paths):
                        self._keep(result, entry, "in_use")
                        continue
                    # Scratch may hide a DB backup (like /tmp/oc5254); configured
                    # caches are regenerable, where names like export.py are code.
                    newest = _newest_mtime(entry, expired=lambda: self.monotonic_fn() > deadline,
                                           scan_protected=True)
                    if newest is None or now - newest < rule["min_age_hours"] * 3600:
                        continue
                    size = tree_bytes(entry)
                    item = {"path": entry, "rule": key, "bytes": size}
                    if cfg["dry_run"]:
                        result["removed"].append({**item, "dry_run": True})
                        continue
                    if not self._receipt({**item, "outcome": "removing"}):
                        self._keep(result, entry, "receipt_unwritable")
                        continue
                    # Re-read safety facts immediately before deleting a cache
                    # entry; a process or backup may have appeared since scan.
                    fresh = self.open_paths_fn()
                    if fresh is None or self._in_use(entry, ancestor_set(fresh)):
                        self._receipt({**item, "outcome": "kept_in_use"})
                        self._keep(result, entry, "in_use")
                        continue
                    if protected_reason(entry, cfg):
                        self._receipt({**item, "outcome": "kept_protected"})
                        self._keep(result, entry, "protected_path")
                        continue
                    if os.path.lexists(os.path.join(entry, ".git")):
                        self._receipt({**item, "outcome": "kept_git_checkout"})
                        self._keep(result, entry, "git_checkout")
                        continue
                    newest = _newest_mtime(entry, expired=lambda: self.monotonic_fn() > deadline,
                                           scan_protected=True)
                    if newest is None or self.time_fn() - newest < rule["min_age_hours"] * 3600:
                        self._receipt({**item, "outcome": "kept_recent_or_unknown"})
                        self._keep(result, entry, "recent_or_unknown")
                        continue
                    try:
                        if os.path.isdir(entry) and not os.path.islink(entry):
                            shutil.rmtree(entry)
                        else:
                            os.unlink(entry)
                        result["removed"].append(item)
                        self._receipt({**item, "outcome": "removed"})
                    except OSError:
                        self._receipt({**item, "outcome": "kept_remove_refused"})
                        self._keep(result, entry, "remove_failed")


def hub_request_from_host_config() -> Callable[[str, str], Any]:
    """GET helper with the host bearer token (same origin/token as disk monitoring)."""
    import urllib.request
    from . import api
    from .disk_monitor import _resolve_origin_and_token

    def request(method: str, path: str) -> Any:
        base, token = _resolve_origin_and_token()
        req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {token}"}, method=method)
        return api.safe_http_json(req, timeout=30.0)

    return request


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run one disk floor janitor pass (intake fcc31e56)")
    parser.add_argument("--config", required=True)
    parser.add_argument("--state-dir")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    with open(args.config, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if args.dry_run:
        raw["dry_run"] = True
    janitor = DiskJanitor(args.config, config=raw, state_dir=args.state_dir or os.path.dirname(os.path.abspath(args.config)),
                          hub_request=hub_request_from_host_config())
    result = janitor.run_once()
    summary = {k: v for k, v in result.items() if k != "kept"}
    summary["kept"] = {reason: len(paths) for reason, paths in result.get("kept", {}).items()}
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
