"""Prepare intake-owned Git workspaces without changing the enrolled checkout."""
import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path

_LOCK = threading.Lock()
_MARKER = 'hub-intake-owner.json'


def _git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                            text=True, timeout=120, check=False,
                            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
    if result.returncode:
        raise ValueError('implement_worktree_git_failed')
    return result.stdout.strip()


def _owner(root):
    admin = Path(_git(root, 'rev-parse', '--absolute-git-dir'))
    marker = admin / _MARKER
    if marker.is_symlink():
        raise ValueError('implement_worktree_owner_invalid')
    if marker.exists():
        return json.loads(marker.read_text())
    return None


def prepare_workspace(workspace, purpose, intake_id, project_id):
    """A bound continuation preserves all commits and dirty files, even offline."""
    root = Path(workspace).resolve(strict=True)
    if purpose in {'review', 'think'}:
        return root
    if purpose not in {None, 'implement', 'fix'}:
        raise ValueError('invalid_session_purpose')
    if not all(isinstance(value, str) and value and len(value) <= 256
               for value in (intake_id, project_id)):
        raise ValueError('implement_worktree_identity_required')
    identity = {'intake_id': intake_id, 'project_id': project_id}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    with _LOCK:
        # A continuation may request the previously bound absolute worktree.
        owner = _owner(root)
        if owner is not None:
            if owner != identity or Path(_git(root, 'rev-parse', '--show-toplevel')).resolve() != root:
                raise ValueError('implement_worktree_owner_conflict')
            return root
        directory = root / '.local' / 'hub-worktrees'
        if not directory.resolve().is_relative_to(root):
            raise ValueError('implement_worktree_path_untrusted')
        target = directory / digest
        if target.is_symlink():
            raise ValueError('implement_worktree_path_untrusted')
        if target.exists():
            if (_owner(target) != identity
                    or Path(_git(target, 'rev-parse', '--show-toplevel')).resolve() != target):
                raise ValueError('implement_worktree_owner_conflict')
            return target
        directory.mkdir(parents=True, exist_ok=True)
        # Fetch the branch explicitly: neither a stale tracking ref nor the
        # enrolled checkout's current feature branch may seed implementation.
        _git(root, 'fetch', '--no-tags', 'origin', '+refs/heads/main:refs/remotes/origin/main')
        base = _git(root, 'rev-parse', '--verify', 'refs/remotes/origin/main^{commit}')
        branch = 'hub/intake-' + digest
        _git(root, 'worktree', 'add', '-b', branch, str(target), base)
        admin = Path(_git(target, 'rev-parse', '--absolute-git-dir'))
        with (admin / _MARKER).open('x') as stream:
            json.dump(identity, stream, sort_keys=True)
        return target
