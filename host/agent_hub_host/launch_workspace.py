"""Prepare engine scratch storage without following links or removing files."""
from pathlib import Path

from . import private_fs


def prepare_tmpdir(workspace):
    """Ensure the exact TMPDIR passed to the maintained launcher exists.

    Explicit workspaces and older Hub servers skip intake worktree creation.
    Engines such as ZCode use TMPDIR directly for their first prompt file.
    The no-follow directory walk preserves existing files and permissions.
    """
    root = Path(workspace)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError('Launcher temporary directory unavailable')
    temp = root / '.local' / 'tmp'
    try:
        # Only parents are created; no sentinel file is written.
        with private_fs.parent(temp / '.launch-directory-check', create=True, private=False):
            pass
    except (OSError, ValueError):
        raise ValueError('Launcher temporary directory unavailable') from None
    return temp
