"""Prepare intake-owned Git workspaces without changing the enrolled checkout."""
from contextlib import contextmanager
import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path

_LOCK = threading.Lock()
_MARKER = 'hub-intake-owner.json'


def _git(root, *args):
    # Never propagate Git's argv/stderr (remote URLs may contain credentials).
    operation = args[0] if args and args[0] in {
        'rev-parse', 'fetch', 'worktree', 'for-each-ref', 'symbolic-ref'
    } else 'other'
    try:
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                                text=True, timeout=120, check=False,
                                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
    except subprocess.TimeoutExpired:
        raise ValueError(f'implement_worktree_git_failed operation={operation} reason=timeout') from None
    except OSError:
        raise ValueError(f'implement_worktree_git_failed operation={operation} reason=unavailable') from None
    if result.returncode:
        raise ValueError(f'implement_worktree_git_failed operation={operation} exit_code={int(result.returncode)}')
    return result.stdout.strip()


@contextmanager
def _repository_lock(root):
    # Unlike the thread lock, this fences independent daemon processes and
    # enrolled checkouts sharing a Git repository. A stale lock fails closed;
    # recovery must never remove a lock whose owner may still be running.
    common = Path(_git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    lock = common / 'hub-implement-workspace.lock'
    try:
        lock.mkdir()
    except FileExistsError:
        raise ValueError('implement_worktree_preparation_busy') from None
    try:
        yield
    finally:
        lock.rmdir()


def _branch_tip(root, ref):
    entries = _git(root, 'for-each-ref', '--format=%(refname) %(objectname) %(symref)', ref)
    lines = [line.split() for line in entries.splitlines() if line.split()[0] == ref]
    if not lines:
        return None
    if len(lines) != 1 or len(lines[0]) != 2:
        raise ValueError('implement_worktree_branch_ambiguous')
    tip = lines[0][1]
    if _git(root, 'rev-parse', '--verify', ref + '^{commit}') != tip:
        raise ValueError('implement_worktree_branch_changed')
    return tip


def _require_unused_branch(root, ref):
    # Include prunable/missing and locked registrations: none may be stolen.
    records = _git(root, 'worktree', 'list', '--porcelain', '-z').split('\0')
    if 'branch ' + ref in records:
        raise ValueError('implement_worktree_branch_in_use')


def _owner(root):
    admin = Path(_git(root, 'rev-parse', '--absolute-git-dir'))
    marker = admin / _MARKER
    if marker.is_symlink():
        raise ValueError('implement_worktree_owner_invalid')
    if marker.exists():
        try:
            return json.loads(marker.read_text())
        except (OSError, ValueError):
            raise ValueError('implement_worktree_owner_invalid') from None
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
        with _repository_lock(root):
            # Recheck after acquiring the cross-process fence.
            if target.is_symlink() or target.exists():
                raise ValueError('implement_worktree_owner_conflict')
            directory.mkdir(parents=True, exist_ok=True)
            branch = 'hub/intake-' + digest
            ref = 'refs/heads/' + branch
            tip = _branch_tip(root, ref)
            if tip is None:
                # Only brand-new intakes seed from freshly fetched main.
                _git(root, 'fetch', '--no-tags', 'origin', '+refs/heads/main:refs/remotes/origin/main')
                base = _git(root, 'rev-parse', '--verify', 'refs/remotes/origin/main^{commit}')
                _git(root, 'worktree', 'add', '-b', branch, str(target), base)
            else:
                # The identity-derived exact ref in this enrolled repository is
                # the retained intake branch. Never reset it, force checkout or
                # prune a registration, even when its path no longer exists.
                _require_unused_branch(root, ref)
                if _branch_tip(root, ref) != tip:
                    raise ValueError('implement_worktree_branch_changed')
                _git(root, 'worktree', 'add', str(target), branch)
                if (_branch_tip(root, ref) != tip
                        or _git(target, 'rev-parse', 'HEAD') != tip
                        or _git(target, 'symbolic-ref', 'HEAD') != ref):
                    # Leave the unmarked partial result for explicit recovery;
                    # never undo a concurrent actor's ref or worktree changes.
                    raise ValueError('implement_worktree_branch_changed')
            admin = Path(_git(target, 'rev-parse', '--absolute-git-dir'))
            with (admin / _MARKER).open('x') as stream:
                json.dump(identity, stream, sort_keys=True)
            return target
