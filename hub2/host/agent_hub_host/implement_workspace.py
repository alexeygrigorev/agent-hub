"""Prepare intake-owned Git workspaces without changing the enrolled checkout."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import stat
import subprocess
import threading
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None

_LOCK = threading.Lock()
_MARKER = 'hub-intake-owner.json'
_OWNERS_DIR = 'hub-intake-owners'
_LOCK_FILE = 'hub-implement-workspace.lock'
_RECORD_VERSION = 1
_RECORD_SOURCES = frozenset({'prepare', 'legacy_worktree_bootstrap', 'operator_legacy_adoption'})


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


def _lock_fd(fd):
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    if msvcrt is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return
    raise OSError('no advisory lock primitive available')


def _unlock_fd(fd):
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    if msvcrt is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


@contextmanager
def _repository_lock(root):
    # Unlike the thread lock, this fences independent daemon processes and
    # enrolled checkouts sharing a Git repository using an OS advisory lock
    # that the kernel automatically releases if the holder dies (e.g. SIGKILL).
    # Lockfile persistence is intentional; never unlink a live lockfile.
    common = Path(_git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    lock = common / _LOCK_FILE
    if lock.is_symlink():
        raise ValueError('implement_worktree_path_untrusted')
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(lock, flags, 0o600)
    except OSError:
        if lock.is_symlink():
            raise ValueError('implement_worktree_path_untrusted') from None
        raise ValueError('implement_worktree_preparation_busy') from None
    try:
        try:
            _lock_fd(fd)
        except OSError:
            raise ValueError('implement_worktree_preparation_busy') from None
        try:
            yield
        finally:
            try:
                _unlock_fd(fd)
            except OSError:
                pass
    finally:
        os.close(fd)


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


def _identity_and_ref(intake_id, project_id):
    if not all(isinstance(value, str) and value and len(value) <= 256
               for value in (intake_id, project_id)):
        raise ValueError('implement_worktree_identity_required')
    identity = {'intake_id': intake_id, 'project_id': project_id}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    ref = 'refs/heads/hub/intake-' + digest
    return identity, digest, ref


def _validate_private_stat(path, *, expect_dir):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        raise ValueError('implement_worktree_owner_invalid') from None
    if stat.S_ISLNK(st.st_mode):
        raise ValueError('implement_worktree_owner_invalid')
    if expect_dir and not stat.S_ISDIR(st.st_mode):
        raise ValueError('implement_worktree_owner_invalid')
    if not expect_dir and not stat.S_ISREG(st.st_mode):
        raise ValueError('implement_worktree_owner_invalid')
    if hasattr(os, 'geteuid') and st.st_uid != os.geteuid():
        raise ValueError('implement_worktree_owner_invalid')
    if os.name != 'nt' and (st.st_mode & 0o022) != 0:
        raise ValueError('implement_worktree_owner_invalid')
    return True


def _read_ownership_record(common, digest, identity, ref):
    owners_dir = common / _OWNERS_DIR
    if not _validate_private_stat(owners_dir, expect_dir=True):
        return None
    record_path = owners_dir / f'{digest}.json'
    if not _validate_private_stat(record_path, expect_dir=False):
        return None
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(record_path, flags)
    except OSError:
        raise ValueError('implement_worktree_owner_invalid') from None
    try:
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        raise ValueError('implement_worktree_owner_invalid') from None
    if (not isinstance(data, dict)
            or data.get('version') != _RECORD_VERSION
            or data.get('source') not in _RECORD_SOURCES
            or not isinstance(data.get('intake_id'), str) or not data['intake_id']
            or not isinstance(data.get('project_id'), str) or not data['project_id']
            or not isinstance(data.get('ref'), str) or not data['ref']):
        raise ValueError('implement_worktree_owner_invalid')
    if (data['intake_id'] != identity['intake_id']
            or data['project_id'] != identity['project_id']
            or data['ref'] != ref):
        raise ValueError('implement_worktree_owner_conflict')
    return data


def _write_ownership_record(common, digest, identity, ref, source, provenance=None):
    owners_dir = common / _OWNERS_DIR
    if not _validate_private_stat(owners_dir, expect_dir=True):
        try:
            os.mkdir(owners_dir, 0o700)
        except FileExistsError:
            pass
        except OSError:
            raise ValueError('implement_worktree_owner_invalid') from None
        if not _validate_private_stat(owners_dir, expect_dir=True):
            raise ValueError('implement_worktree_owner_invalid')
    record_path = owners_dir / f'{digest}.json'
    if _validate_private_stat(record_path, expect_dir=False):
        raise ValueError('implement_worktree_owner_conflict')
    payload = {
        'version': _RECORD_VERSION,
        'intake_id': identity['intake_id'],
        'project_id': identity['project_id'],
        'ref': ref,
        'source': source,
    }
    if provenance is not None:
        payload['provenance'] = provenance
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(record_path, flags, 0o600)
    except FileExistsError:
        raise ValueError('implement_worktree_owner_conflict') from None
    except OSError:
        raise ValueError('implement_worktree_owner_invalid') from None
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(payload, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    return record_path


def _ensure_legacy_worktree_record(root, digest, identity, ref):
    common = Path(_git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    if _read_ownership_record(common, digest, identity, ref) is not None:
        return
    with _repository_lock(root):
        if _read_ownership_record(common, digest, identity, ref) is None:
            _write_ownership_record(common, digest, identity, ref, 'legacy_worktree_bootstrap')


def _validate_provenance_field(value, max_len):
    if not isinstance(value, str):
        raise ValueError('implement_worktree_provenance_invalid')
    cleaned = value.strip()
    if not cleaned or len(cleaned) > max_len:
        raise ValueError('implement_worktree_provenance_invalid')
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValueError('implement_worktree_provenance_invalid')
    lowered = cleaned.lower()
    if 'bearer ' in lowered or ('://' in lowered and '@' in lowered):
        raise ValueError('implement_worktree_provenance_invalid')
    return cleaned


def adopt_legacy_intake_branch(
    workspace,
    intake_id,
    project_id,
    expected_ref,
    expected_tip,
    *,
    operator,
    reason,
):
    """Explicit operator-only CAS adoption for a legacy retained intake branch."""
    root = Path(workspace).resolve(strict=True)
    identity, digest, derived_ref = _identity_and_ref(intake_id, project_id)
    if not isinstance(expected_ref, str) or expected_ref != derived_ref:
        raise ValueError('implement_worktree_ref_mismatch')
    if not isinstance(expected_tip, str) or not expected_tip:
        raise ValueError('implement_worktree_tip_mismatch')
    clean_operator = _validate_provenance_field(operator, 128)
    clean_reason = _validate_provenance_field(reason, 256)
    with _LOCK:
        directory = root / '.local' / 'hub-worktrees'
        if directory.exists():
            if not directory.resolve().is_relative_to(root):
                raise ValueError('implement_worktree_path_untrusted')
        target = directory / digest
        if target.is_symlink():
            raise ValueError('implement_worktree_path_untrusted')
        if target.exists():
            raise ValueError('implement_worktree_owner_conflict')
        with _repository_lock(root):
            if target.is_symlink():
                raise ValueError('implement_worktree_path_untrusted')
            if target.exists():
                raise ValueError('implement_worktree_owner_conflict')
            common = Path(_git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
            existing = _read_ownership_record(common, digest, identity, derived_ref)
            actual_tip = _branch_tip(root, derived_ref)
            if actual_tip is None:
                raise ValueError('implement_worktree_branch_missing')
            if actual_tip != expected_tip:
                raise ValueError('implement_worktree_tip_mismatch')
            _require_unused_branch(root, derived_ref)
            if existing is not None:
                record_path = common / _OWNERS_DIR / f'{digest}.json'
                return {
                    'record_path': str(record_path),
                    'ref': derived_ref,
                    'tip': actual_tip,
                    'source': existing['source'],
                    'already_recorded': True,
                }
            provenance = {
                'operator': clean_operator,
                'reason': clean_reason,
                'adopted_tip': actual_tip,
                'adopted_at': datetime.now(timezone.utc).isoformat(),
            }
            record_path = _write_ownership_record(
                common,
                digest,
                identity,
                derived_ref,
                'operator_legacy_adoption',
                provenance=provenance,
            )
            return {
                'record_path': str(record_path),
                'ref': derived_ref,
                'tip': actual_tip,
                'source': 'operator_legacy_adoption',
                'already_recorded': False,
            }


def prepare_workspace(workspace, purpose, intake_id, project_id):
    """A bound continuation preserves all commits and dirty files, even offline."""
    root = Path(workspace).resolve(strict=True)
    if purpose in {'review', 'think'}:
        return root
    if purpose not in {None, 'implement', 'fix'}:
        raise ValueError('invalid_session_purpose')
    identity, digest, ref = _identity_and_ref(intake_id, project_id)
    branch = 'hub/intake-' + digest
    with _LOCK:
        # A continuation may request the previously bound absolute worktree.
        owner = _owner(root)
        if owner is not None:
            if owner != identity or Path(_git(root, 'rev-parse', '--show-toplevel')).resolve() != root:
                raise ValueError('implement_worktree_owner_conflict')
            _ensure_legacy_worktree_record(root, digest, identity, ref)
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
            _ensure_legacy_worktree_record(target, digest, identity, ref)
            return target
        with _repository_lock(root):
            # Recheck after acquiring the cross-process fence.
            if target.is_symlink():
                raise ValueError('implement_worktree_path_untrusted')
            if target.exists():
                raise ValueError('implement_worktree_owner_conflict')
            common = Path(_git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
            record = _read_ownership_record(common, digest, identity, ref)
            directory.mkdir(parents=True, exist_ok=True)
            tip = _branch_tip(root, ref)
            if tip is None:
                # Only brand-new intakes seed from freshly fetched main.
                _git(root, 'fetch', '--no-tags', 'origin', '+refs/heads/main:refs/remotes/origin/main')
                base = _git(root, 'rev-parse', '--verify', 'refs/remotes/origin/main^{commit}')
                if record is None:
                    _write_ownership_record(common, digest, identity, ref, 'prepare')
                _git(root, 'worktree', 'add', '-b', branch, str(target), base)
            else:
                # A retained branch must have a durable common-dir ownership
                # record; branch name alone never proves ownership.
                if record is None:
                    raise ValueError('implement_worktree_ownership_unverified')
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
