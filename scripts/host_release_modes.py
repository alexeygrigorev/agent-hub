"""Permission checks for immutable Agent Hub host release trees."""

import os
import stat
from pathlib import Path

GO_WRITE = 0o022
DEFAULT_RELEASES_ROOT = Path("/home/alexey/.local/share/agent-hub/host-releases")


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
    """Remove group/world write bits from regular files and directories only."""
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


def validate_installed_release(release, releases_root=DEFAULT_RELEASES_ROOT):
    """Confine hardening to one real, manifest-bearing installed release."""
    release = Path(release)
    root = Path(releases_root)
    if root.is_symlink() or release.is_symlink():
        raise ValueError("release and releases root must not be symlinks")
    root = root.resolve(strict=True)
    target = release.resolve(strict=True)
    if target.parent != root or not target.is_dir():
        raise ValueError("release must be a direct child of the configured releases root")
    manifest = target / "release-manifest.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("release-manifest.json is required")
    if not stat.S_ISREG(manifest.stat(follow_symlinks=False).st_mode):
        raise ValueError("release-manifest.json must be a regular file")
    return target
