"""Repo-identity workspaces (intake 8028bb06).

A Hub start that names a repository (``--repo owner/name``) instead of a host
path carries the workspace id ``repo-<id8>-<owner>__<name>`` (``.`` in the name
is written ``_d_``; mirrors ``encodeRepoWorkspace`` in src/fleet.ts). It is not
a path: this host clones the repository into its own work root,

    <work_root>/<id8>/<name>        (Windows: %USERPROFILE%\\work\\<id8>\\<name>)

checks out a fresh branch from ``origin/main`` and uses that directory as the
workspace. Nothing is ever deleted; an existing checkout is reused.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

REPO_WORKSPACE = re.compile(r"repo-([0-9a-f]{8})-([A-Za-z0-9][A-Za-z0-9-]{0,38})__([A-Za-z0-9_-]{1,60})\Z")
REPO_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}\Z")
CLONE_TIMEOUT = 900.0
GIT_TIMEOUT = 120.0


def decode(label) -> Optional[tuple[str, str, str]]:
    """``(id8, owner, name)`` for a repo-identity workspace id, else None."""
    if not isinstance(label, str):
        return None
    m = REPO_WORKSPACE.fullmatch(label)
    if not m:
        return None
    name = m.group(3).replace("_d_", ".")
    if not REPO_NAME.fullmatch(name) or name.startswith("."):
        return None
    return m.group(1), m.group(2), name


def work_root(settings, home: Optional[str] = None) -> Path:
    """Host work root: settings ``work_root`` or ``<home>/work`` (home = %USERPROFILE% on Windows)."""
    configured = (settings or {}).get("work_root")
    if isinstance(configured, str) and configured and os.path.isabs(configured):
        return Path(configured)
    return Path(home if home is not None else Path.home()) / "work"


def repo_path(settings, label, home: Optional[str] = None) -> Path:
    parts = decode(label)
    if parts is None:
        raise ValueError("Local account/profile/workspace is not configured")
    return work_root(settings, home) / parts[0] / parts[2]


def _run(argv: list[str], cwd: Optional[str], timeout: float) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true", LC_ALL="C")
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                          cwd=cwd, env=env, stdin=subprocess.DEVNULL)


def _short(proc) -> str:
    for line in (proc.stderr or proc.stdout or "").splitlines():
        if line.strip():
            return line.strip()[:200]
    return f"exit {proc.returncode}"


def resolve_existing(settings, label, home: Optional[str] = None) -> Path:
    """The already materialised checkout; ValueError when it is absent."""
    path = repo_path(settings, label, home)
    if not path.is_dir() or path.is_symlink():
        raise ValueError("Local account/profile/workspace is not configured")
    return path.resolve()


def ensure_repo_workspace(settings, label, *, home: Optional[str] = None,
                          runner: Optional[Callable] = None, git_bin: str = "git",
                          gh_bin: Optional[str] = None) -> Path:
    """Clone the repo into this host's work root (once) and return the checkout."""
    parts = decode(label)
    if parts is None:
        raise ValueError("Local account/profile/workspace is not configured")
    id8, owner, name = parts
    run = runner or _run
    path = repo_path(settings, label, home)
    if path.is_dir():
        if (path / ".git").exists():
            return path.resolve()
        raise ValueError("workspace path exists and is not a git checkout")
    path.parent.mkdir(parents=True, exist_ok=True)
    slug = f"{owner}/{name}"
    gh = gh_bin if gh_bin is not None else shutil.which("gh")
    proc = None
    if gh:
        proc = run([gh, "repo", "clone", slug, str(path)], None, CLONE_TIMEOUT)
    if proc is None or proc.returncode != 0:
        proc = run([git_bin, "clone", "--quiet", f"https://github.com/{slug}.git", str(path)], None, CLONE_TIMEOUT)
    if proc.returncode != 0:
        raise ValueError("repo clone failed: " + _short(proc))
    for argv in ([git_bin, "fetch", "--quiet", "origin", "main"],
                 [git_bin, "checkout", "--quiet", "-B", "wt-" + id8, "origin/main"]):
        proc = run(argv, str(path), GIT_TIMEOUT)
        if proc.returncode != 0:
            raise ValueError(f"git {argv[1]} failed: " + _short(proc))
    return path.resolve()
