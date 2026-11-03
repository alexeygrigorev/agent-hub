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

from . import git_noninteractive

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
    env = git_noninteractive.noninteractive_env()
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
        if not any(path.iterdir()):
            path.rmdir()  # empty leftover of an interrupted clone; nothing to lose
        else:
            raise ValueError("workspace path exists and is not a git checkout")
    path.parent.mkdir(parents=True, exist_ok=True)
    slug = f"{owner}/{name}"
    gh = gh_bin if gh_bin is not None else git_noninteractive.find_gh()
    # Preflight (intake hubfail-t2596u): a dead loopback proxy is the observed
    # Hub failure mode (desktop HTTPS_PROXY with no listener). Name it as the
    # reason instead of a bare clone failure; the message carries only a
    # sanitized host:port, never credentials. gh auth is advisory here (public
    # repos clone without it) and joins the failure tail below. A proxy
    # bypassed for every needed GitHub host via NO_PROXY (the operator
    # workaround for codex-vpn) is left alone; nothing here writes env.
    wanted = ((git_noninteractive._GITHUB_SUFFIXES + git_noninteractive._GH_EXTRA_HOSTS)
              if gh else git_noninteractive._GITHUB_SUFFIXES)
    proxy_finding = git_noninteractive.dead_proxy_finding(hosts=wanted)
    if proxy_finding is not None:
        raise ValueError("repo_proxy_unreachable: " + slug + ": " + proxy_finding)
    proc = None
    if gh:
        proc = run([gh, "repo", "clone", slug, str(path)], None, CLONE_TIMEOUT)
    if proc is None or proc.returncode != 0:
        proc = run([git_bin, "clone", "--quiet", f"https://github.com/{slug}.git", str(path)], None, CLONE_TIMEOUT)
    if proc.returncode != 0:
        detail = (proc.stderr or "") + (proc.stdout or "")
        gh_finding = git_noninteractive.gh_auth_finding(gh_bin if gh_bin is not None else gh)
        tail = _short(proc) + ("" if not gh_finding else "; " + gh_finding[:160])
        if git_noninteractive.is_repo_not_found(detail):
            raise ValueError("repo_not_found: " + slug + ": " + tail)
        if git_noninteractive.is_auth_failure(detail):
            raise ValueError(git_noninteractive.AUTH_FAILED + ": " + slug + ": " + tail)
        raise ValueError("repo_clone_failed: " + slug + ": " + tail)
    for argv in ([git_bin, "fetch", "--quiet", "origin", "main"],
                 [git_bin, "checkout", "--quiet", "-B", "wt-" + id8, "origin/main"]):
        proc = run(argv, str(path), GIT_TIMEOUT)
        if proc.returncode != 0:
            if git_noninteractive.is_auth_failure((proc.stderr or "") + (proc.stdout or "")):
                raise ValueError(git_noninteractive.AUTH_FAILED + f": git {argv[1]}: " + _short(proc))
            raise ValueError(f"git {argv[1]} failed: " + _short(proc))
    return path.resolve()
