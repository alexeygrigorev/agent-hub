"""Permission checks for immutable Agent Hub host release trees."""

import json
import os
import re
import stat
from pathlib import Path

GO_WRITE = 0o022
DEFAULT_RELEASES_ROOTS = (
    Path("/home/alexey/.local/share/agent-hub/host-releases"),
    Path("/home/alexey/.local/share/agent-hub/host-releases-orch"),
)


def _entries(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("release root must be a real directory")
    yield root
    for current, dirs, files in os.walk(root, followlinks=False):
        base = Path(current)
        for name in dirs:
            path = base / name
            if path.is_symlink():
                continue
            yield path
        for name in files:
            path = base / name
            if path.is_symlink():
                continue
            yield path


def verify_tree(root):
    """Raise if any non-symlink release file or directory is group/world writable."""
    writable = []
    for path in _entries(root):
        mode = path.stat(follow_symlinks=False).st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ValueError("release contains a special file: " + str(path))
        if mode & GO_WRITE:
            writable.append(str(path))
    if writable:
        raise ValueError("group/world-writable release entries: " + ", ".join(writable[:20]))
    return True


def harden_tree(root):
    """Remove group/world write bits from regular files and directories only.

    Only permission bits are changed. Note: chmod follows the inode, so a
    hard-linked file (e.g. uv cache hardlinks into a release .venv) also
    loses go-w on the shared inode outside the tree. That only removes bits,
    never content, so it is safe.
    """
    changed = []
    for path in _entries(root):
        mode = path.stat(follow_symlinks=False).st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ValueError("release contains a special file: " + str(path))
        if mode & GO_WRITE:
            os.chmod(path, stat.S_IMODE(mode) & ~GO_WRITE, follow_symlinks=False)
            changed.append(path)
    verify_tree(root)
    return changed


def validate_installed_release(release, releases_roots=DEFAULT_RELEASES_ROOTS):
    """Confine hardening to one real, manifest-bearing installed release."""
    release = Path(release)
    if isinstance(releases_roots, (str, Path)):
        releases_roots = (releases_roots,)
    roots = tuple(Path(root) for root in releases_roots)
    if release.is_symlink() or any(root.is_symlink() for root in roots):
        raise ValueError("release and releases roots must not be symlinks")
    roots = tuple(root.resolve(strict=True) for root in roots)
    target = release.resolve(strict=True)
    if target.parent not in roots or not target.is_dir():
        raise ValueError("release must be a direct child of a managed releases root")
    if not re.fullmatch(r"[0-9a-f]{40}", target.name):
        raise ValueError("release directory name must be a full lowercase commit SHA")
    manifest = target / "release-manifest.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("release-manifest.json is required")
    if not stat.S_ISREG(manifest.stat(follow_symlinks=False).st_mode):
        raise ValueError("release-manifest.json must be a regular file")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("release-manifest.json must be valid JSON") from exc
    if not isinstance(data, dict) or data.get("release_rev") != target.name:
        raise ValueError("release manifest SHA must match its directory name")
    return target
