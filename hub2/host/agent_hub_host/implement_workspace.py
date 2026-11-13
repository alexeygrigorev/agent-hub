"""Prepare intake-owned Git workspaces without changing the enrolled checkout."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import re
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
_HUB_SESSION_LOCK_RE = re.compile(
    r'^hub-session ([0-9a-fA-F-]{8,64}) intake ([A-Za-z0-9][A-Za-z0-9._:-]{0,127}|none)$'
)
# Version 2 records are only ever published once ownership is complete
# (phase "owned"); earlier pre-creation records are rejected fail-closed.
_RECORD_VERSION = 2
_RECORD_PHASE = 'owned'
_RECORD_SOURCES = frozenset({'prepare', 'legacy_worktree_bootstrap', 'operator_legacy_adoption'})


def _run_git(root, args, allowed=(0,)):
    # Never propagate Git's argv/stderr (remote URLs may contain credentials).
    operation = args[0] if args and args[0] in {
        'rev-parse', 'fetch', 'worktree', 'for-each-ref', 'symbolic-ref', 'ls-remote'
    } else 'other'
    try:
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                                text=True, timeout=120, check=False,
                                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
    except subprocess.TimeoutExpired:
        raise ValueError(f'implement_worktree_git_failed operation={operation} reason=timeout') from None
    except OSError:
        raise ValueError(f'implement_worktree_git_failed operation={operation} reason=unavailable') from None
    if result.returncode not in allowed:
        raise ValueError(f'implement_worktree_git_failed operation={operation} exit_code={int(result.returncode)}')
    return result.returncode, result.stdout.strip()


def _git(root, *args):
    return _run_git(root, args)[1]


def _symbolic_head(root):
    """Return the exact symbolic HEAD ref, or None when HEAD is detached."""
    code, out = _run_git(root, ('symbolic-ref', '-q', 'HEAD'), allowed=(0, 1))
    return out if code == 0 and out else None


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


def _worktree_entries(root):
    """Parse `git worktree list --porcelain -z` into a list of worktree dicts."""
    raw = _run_git(root, ('worktree', 'list', '--porcelain', '-z'))[1]
    entries = []
    current = None
    for token in raw.split('\0'):
        if not token:
            if current is not None:
                entries.append(current)
                current = None
            continue
        if token.startswith('worktree '):
            if current is not None:
                entries.append(current)
            current = {
                'path': token[len('worktree '):],
                'branch': None,
                'locked': False,
                'lock_reason': None,
                'prunable': False,
            }
            continue
        if current is None:
            continue
        if token.startswith('branch '):
            current['branch'] = token[len('branch '):]
        elif token == 'locked':
            current['locked'] = True
        elif token.startswith('locked '):
            current['locked'] = True
            current['lock_reason'] = token[len('locked '):].strip() or None
        elif token == 'prunable' or token.startswith('prunable '):
            current['prunable'] = True
    if current is not None:
        entries.append(current)
    return entries


def _parse_hub_lock(reason):
    if not reason:
        return None
    match = _HUB_SESSION_LOCK_RE.fullmatch(reason.strip())
    if not match:
        return None
    intake = None if match.group(2) == 'none' else match.group(2)
    return match.group(1), intake


def _release_stale_lock(root, entry, session_is_dead):
    """Unlock a worktree when its Hub session lock belongs to a dead session.

    Returns True if unlocked (or not locked), False if still locked.
    """
    if not entry.get('locked'):
        return True
    parsed = _parse_hub_lock(entry.get('lock_reason'))
    if parsed and session_is_dead is not None:
        sid, _intake = parsed
        try:
            dead = bool(session_is_dead(sid))
        except Exception:
            dead = False
        if dead:
            _git(root, 'worktree', 'unlock', entry['path'])
            entry['locked'] = False
            entry['lock_reason'] = None
            return True
    return False


def _lock_detail(entry):
    if not entry.get('locked'):
        return ''
    parsed = _parse_hub_lock(entry.get('lock_reason'))
    if parsed:
        return f' lock_session={parsed[0]}'
    reason = entry.get('lock_reason') or 'locked'
    return f' lock={reason[:80]}'


def _prunable_owned_by_identity(common, worktree_path, identity):
    """True only when a prunable worktree's admin dir carries a matching owner marker."""
    worktrees_dir = common / 'worktrees'
    if not worktrees_dir.is_dir():
        return False
    norm_target = os.path.normpath(worktree_path)
    for admin in worktrees_dir.iterdir():
        if not admin.is_dir() or admin.is_symlink():
            continue
        gitdir_file = admin / 'gitdir'
        marker = admin / _MARKER
        if not gitdir_file.is_file() or not marker.is_file() or marker.is_symlink():
            continue
        try:
            recorded_gitdir = gitdir_file.read_text(encoding='utf-8').strip()
        except OSError:
            continue
        recorded_wt = os.path.normpath(os.path.dirname(recorded_gitdir))
        if recorded_wt != norm_target:
            continue
        try:
            return json.loads(marker.read_text(encoding='utf-8')) == identity
        except (OSError, ValueError):
            return False
    return False


def _default_origin_branch(root):
    """Resolve the default branch name on origin (e.g. main or master)."""
    code, sym = _run_git(root, ('symbolic-ref', '-q', 'refs/remotes/origin/HEAD'), allowed=(0, 1))
    prefix = 'refs/remotes/origin/'
    if code == 0 and sym.startswith(prefix) and len(sym) > len(prefix):
        return sym[len(prefix):]
    ls_out = _git(root, 'ls-remote', '--symref', 'origin', 'HEAD')
    for line in ls_out.splitlines():
        if line.startswith('ref: refs/heads/'):
            target = line[len('ref: refs/heads/'):].split()[0]
            if target:
                return target
    for candidate in ('main', 'master'):
        heads = _git(root, 'ls-remote', '--heads', 'origin', f'refs/heads/{candidate}')
        if heads:
            return candidate
    raise ValueError('implement_worktree_git_failed operation=ls-remote reason=no_default_branch')


def _require_unused_branch(root, ref):
    # Include prunable/missing and locked registrations: none may be stolen.
    for entry in _worktree_entries(root):
        if entry.get('branch') == ref:
            status = ' status=prunable' if entry.get('prunable') else ''
            raise ValueError(
                f'implement_worktree_branch_in_use worktree={entry["path"]}{_lock_detail(entry)}{status}'
            )


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
            or data.get('phase') != _RECORD_PHASE
            or not isinstance(data.get('intake_id'), str) or not data['intake_id']
            or not isinstance(data.get('project_id'), str) or not data['project_id']
            or not isinstance(data.get('ref'), str) or not data['ref']):
        raise ValueError('implement_worktree_owner_invalid')
    if (data['intake_id'] != identity['intake_id']
            or data['project_id'] != identity['project_id']
            or data['ref'] != ref):
        raise ValueError('implement_worktree_owner_conflict')
    return data


def _fsync_dir(path, *, nofollow):
    if os.name == 'nt':  # pragma: no cover - directories cannot be fsynced
        return
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
    if nofollow:
        flags |= getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _record_durability_open_flags(*, windows):
    """Return non-creating flags suitable for flushing an existing record."""
    access = os.O_RDWR if windows else os.O_RDONLY
    return access | getattr(os, 'O_NOFOLLOW', 0)


def _make_record_durable(common, digest):
    """Flush the record and, on POSIX, every directory entry leading to it.

    Called before any successful return that relies on the record, including
    retries that find an already existing record. POSIX directory fsyncs make
    the record's path updates durable; Windows only flushes the record because
    directory fsync is unavailable there and does not get the same barrier.
    """
    owners_dir = common / _OWNERS_DIR
    record_path = owners_dir / f'{digest}.json'
    if (not _validate_private_stat(owners_dir, expect_dir=True)
            or not _validate_private_stat(record_path, expect_dir=False)):
        raise ValueError('implement_worktree_owner_invalid')
    try:
        flags = _record_durability_open_flags(windows=os.name == 'nt')
        fd = os.open(record_path, flags)
    except OSError:
        raise ValueError('implement_worktree_owner_invalid') from None
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode)
                or (hasattr(os, 'geteuid') and st.st_uid != os.geteuid())):
            raise ValueError('implement_worktree_owner_invalid')
        try:
            os.fsync(fd)
        except OSError:
            raise ValueError('implement_worktree_owner_durability_failed') from None
    finally:
        os.close(fd)
    try:
        _fsync_dir(owners_dir, nofollow=True)
        _fsync_dir(common, nofollow=False)
    except OSError:
        raise ValueError('implement_worktree_owner_durability_failed') from None
    return record_path


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
        'phase': _RECORD_PHASE,
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
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise ValueError('implement_worktree_owner_durability_failed') from None
    return _make_record_durable(common, digest)


def _head_binds_ref(root, ref):
    """True only if the worktree's symbolic HEAD is exactly the derived ref."""
    try:
        if _symbolic_head(root) != ref:
            return False
        tip = _branch_tip(root, ref)
        return tip is not None and _git(root, 'rev-parse', '--verify', 'HEAD^{commit}') == tip
    except ValueError:
        return False


def _ensure_legacy_worktree_record(root, digest, identity, ref):
    """Make an existing record durable, or bootstrap one from a bound marker.

    A marked continuation is always preserved (dirty files included), but a
    record is only minted when HEAD is still symbolically bound to the exact
    derived ref; a renamed or detached legacy worktree never authorizes a
    different same-name ref.
    """
    common = Path(_git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    if _read_ownership_record(common, digest, identity, ref) is not None:
        _make_record_durable(common, digest)
        return
    if not _head_binds_ref(root, ref):
        return
    with _repository_lock(root):
        if _read_ownership_record(common, digest, identity, ref) is not None:
            _make_record_durable(common, digest)
        elif _head_binds_ref(root, ref):
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
                record_path = _make_record_durable(common, digest)
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


def _ensure_unlocked_worktree(root, target_path, session_is_dead):
    resolved = str(Path(target_path).resolve())
    for entry in _worktree_entries(root):
        try:
            entry_resolved = str(Path(entry['path']).resolve())
        except OSError:
            entry_resolved = entry['path']
        if entry_resolved == resolved and entry.get('locked'):
            if not _release_stale_lock(root, entry, session_is_dead):
                raise ValueError(f'implement_worktree_locked worktree={entry["path"]}{_lock_detail(entry)}')


def prepare_workspace(workspace, purpose, intake_id, project_id, *, session_is_dead=None, allowed_roots=None):
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
                raise ValueError(f'implement_worktree_owner_conflict worktree={root}')
            _ensure_unlocked_worktree(root, root, session_is_dead)
            _ensure_legacy_worktree_record(root, digest, identity, ref)
            return root
        directory = root / '.local' / 'hub-worktrees'
        if not directory.resolve().is_relative_to(root):
            raise ValueError(f'implement_worktree_path_untrusted worktree={directory}')
        target = directory / digest
        if target.is_symlink():
            raise ValueError(f'implement_worktree_path_untrusted worktree={target}')
        if target.exists():
            if (_owner(target) != identity
                    or Path(_git(target, 'rev-parse', '--show-toplevel')).resolve() != target):
                raise ValueError(f'implement_worktree_owner_conflict worktree={target}')
            _ensure_unlocked_worktree(root, target, session_is_dead)
            _ensure_legacy_worktree_record(target, digest, identity, ref)
            return target
        with _repository_lock(root):
            # Recheck after acquiring the cross-process fence.
            if target.is_symlink():
                raise ValueError(f'implement_worktree_path_untrusted worktree={target}')
            if target.exists():
                raise ValueError(f'implement_worktree_owner_conflict worktree={target}')
            common = Path(_git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
            record = _read_ownership_record(common, digest, identity, ref)
            directory.mkdir(parents=True, exist_ok=True)
            tip = _branch_tip(root, ref)
            if tip is None:
                # Only brand-new intakes seed from the freshly fetched default branch.
                default_branch = _default_origin_branch(root)
                _git(root, 'fetch', '--no-tags', 'origin',
                     f'+refs/heads/{default_branch}:refs/remotes/origin/{default_branch}')
                base = _git(root, 'rev-parse', '--verify',
                            f'refs/remotes/origin/{default_branch}^{{commit}}')
                # `-b` refuses an existing ref, so a foreign ref created after
                # the tip check makes this fail with no ownership published.
                _git(root, 'worktree', 'add', '-b', branch, str(target), base)
                if (_branch_tip(root, ref) != base
                        or _git(target, 'rev-parse', 'HEAD') != base
                        or _symbolic_head(target) != ref):
                    # Unmarked and unrecorded: retries fail closed until an
                    # operator verifies; never undo a concurrent actor.
                    raise ValueError('implement_worktree_branch_changed')
                # Ownership is published only after our own creation is proven.
                if record is None:
                    _write_ownership_record(common, digest, identity, ref, 'prepare')
                else:
                    _make_record_durable(common, digest)
            else:
                # A retained branch must have a durable common-dir ownership
                # record; branch name alone never proves ownership.
                if record is None:
                    raise ValueError(f'implement_worktree_ownership_unverified ref={ref}')
                _make_record_durable(common, digest)
                for entry in _worktree_entries(root):
                    if entry.get('branch') != ref:
                        continue
                    if not _release_stale_lock(root, entry, session_is_dead):
                        raise ValueError(
                            f'implement_worktree_branch_in_use worktree={entry["path"]}{_lock_detail(entry)}'
                        )
                    if entry.get('prunable') or not Path(entry['path']).exists():
                        if _prunable_owned_by_identity(common, entry['path'], identity):
                            _git(root, 'worktree', 'prune')
                        else:
                            raise ValueError(
                                f'implement_worktree_branch_in_use worktree={entry["path"]} status=prunable'
                            )
                        continue
                    wt_path = Path(entry['path'])
                    if wt_path.is_dir() and not wt_path.is_symlink():
                        wt_resolved = wt_path.resolve()
                        in_roots = (allowed_roots is None
                                    or any(wt_resolved.is_relative_to(r) for r in allowed_roots))
                        if (in_roots
                                and _owner(wt_resolved) == identity
                                and Path(_git(wt_resolved, 'rev-parse', '--show-toplevel')).resolve() == wt_resolved):
                            _ensure_legacy_worktree_record(wt_resolved, digest, identity, ref)
                            return wt_resolved
                    raise ValueError(f'implement_worktree_branch_in_use worktree={entry["path"]}')
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
