"""Cooperating Linux regular-CLI writers; never enroll an existing pathname.

Ownership is established only by this publisher creating an absent target.
A held-FD recheck/exchange detects races; it is not kernel CAS against same-UID
uncooperative writers. Displaced original inodes and uncertain intents remain.
"""
import ast
import contextlib
import ctypes
import errno
try:
    import fcntl
except ImportError:  # The new writer is Linux-only; Windows legacy CLI is unchanged.
    fcntl = None
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid
import zipfile

MAX = 32 * 1024 * 1024
OWNER = 'regular-cli-owner.json'
PENDING = 'regular-cli-pending.json'


class Refused(RuntimeError):
    pass


class _Current(Exception):
    pass


class Budget:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.start = self.last = clock()
        self.check()
    def check(self):
        now = self.clock()
        if (type(now) not in (int, float) or not math.isfinite(now)
                or now < self.last or now - self.start >= 20):
            raise Refused('regular_cli_deadline')
        self.last = now


class FDs:
    def __init__(self): self.values = []
    def add(self, fd): self.values.append(fd); return fd
    def close(self):
        errors = []
        while self.values:
            fd = self.values.pop()  # Consume BEFORE close, including uncertain failures.
            try: os.close(fd)
            except BaseException as exc: errors.append(exc)
        if errors: raise errors[0]


def identity(st):
    return {k: getattr(st, 'st_' + k) for k in ('dev', 'ino', 'uid', 'gid', 'mode', 'nlink', 'size')}


def directory(path, fds, create=False):
    path = Path(path)
    if not path.is_absolute() or any(x in ('.', '..') for x in path.parts):
        raise Refused('regular_cli_directory')
    fd = fds.add(os.open('/', os.O_RDONLY | os.O_DIRECTORY))
    for part in path.parts[1:]:
        if create:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
                os.fsync(fd)  # New child name durable before any selected-target publication.
            except FileExistsError: pass
        fd = fds.add(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd))
        st = os.fstat(fd)
        if st.st_uid not in (0, os.getuid()) or st.st_mode & 0o002:
            raise Refused('regular_cli_directory')
    return fd


def file_bytes(parent, name, fds, limit=MAX):
    fd = fds.add(os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent))
    st = os.fstat(fd)
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
            or st.st_nlink != 1 or st.st_mode & 0o022 or not 0 < st.st_size <= limit):
        raise Refused('regular_cli_file')
    chunks = []
    while True:
        part = os.read(fd, min(65536, limit + 1 - sum(map(len, chunks))))
        if not part: break
        chunks.append(part)
        if sum(map(len, chunks)) > limit: raise Refused('regular_cli_bound')
    data = b''.join(chunks)
    if identity(os.fstat(fd)) != identity(st) or len(data) != st.st_size:
        raise Refused('regular_cli_changed')
    return fd, data, identity(st)


def same_name(parent, name, expected):
    try: return identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) == expected
    except FileNotFoundError: return False


def verified_name(parent, name, expected, checksum, fds):
    try:
        _, data, actual = file_bytes(parent,name,fds)
        return actual==expected and digest(data)==checksum and same_name(parent,name,expected)
    except FileNotFoundError:return False


def digest(data): return hashlib.sha256(data).hexdigest()


def verified(data, manifest):
    record = manifest.get('artifacts', {}).get('hub.pyz', {})
    generation = manifest.get('host_version')
    if (not re.fullmatch('[0-9a-f]{40}', str(generation))
            or type(record.get('size')) is not int or not 0 < record['size'] <= MAX
            or record['size'] != len(data) or record.get('sha256') != digest(data)):
        raise Refused('regular_cli_artifact')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = [x for x in archive.infolist() if x.filename == 'agent_hub_host/_build_info.py']
        if len(entries) != 1 or entries[0].file_size > 4096:
            raise Refused('regular_cli_generation')
        tree = ast.parse(archive.read(entries[0]))
    commits = [ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == 'BUILD_COMMIT' for t in n.targets)]
    if commits != [generation]: raise Refused('regular_cli_generation')
    return {'sha256': digest(data), 'size': len(data), 'generation': generation}


def exchange(parent, left, right, flags=2):
    library = ctypes.CDLL(None, use_errno=True)
    fn = library.renameat2
    fn.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    fn.restype = ctypes.c_int
    if fn(parent, os.fsencode(left), parent, os.fsencode(right), flags):
        value = ctypes.get_errno()
        raise OSError(value, os.strerror(value))


def write_all(fd, data, budget):
    offset=0
    while offset<len(data):
        budget.check()
        written=os.write(fd,data[offset:offset+65536])
        if written<=0:raise OSError('regular_cli_short_write')
        offset+=written
    os.fsync(fd)


def json_bytes(data): return (json.dumps(data, sort_keys=True) + '\n').encode()


def read_record(parent, name, fds):
    try: _, data, _ = file_bytes(parent, name, fds, 65536)
    except FileNotFoundError: return None
    def unique(items):
        result = {}
        for key, value in items:
            if key in result: raise Refused('regular_cli_record')
            result[key] = value
        return result
    value=json.loads(data, object_pairs_hook=unique)
    if not isinstance(value,dict):raise Refused('regular_cli_record')
    return value


def write_record(parent, name, record, fds, budget, exclusive=False):
    budget.check()
    temporary = '.regular-record-' + str(uuid.uuid4())
    fd = fds.add(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600, dir_fd=parent))
    data = json_bytes(record)
    write_all(fd,data,budget)
    budget.check()  # Journal fsync cannot authorize a late namespace publication.
    if exclusive: exchange(parent, temporary, name, 1)
    else: os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    os.fsync(parent)


def owner_matches(owner, target, parent, current):
    if not isinstance(owner,dict) or set(owner)!={'schema','target','parent','file','artifact'}:
        return False
    fields={'dev','ino','uid','gid','mode','nlink','size'}
    if (type(owner['schema']) is not int or owner['schema']!=1 or owner['target']!=str(target)
            or not isinstance(current,dict) or not isinstance(owner['file'],dict)
            or set(owner['file'])!=fields or any(type(x) is not int for x in owner['file'].values())
            or owner['file']!=current or not isinstance(owner['parent'],dict)
            or any(type(x) is not int for x in owner['parent'].values())
            or owner['parent']!={k:identity(os.fstat(parent))[k] for k in ('dev','ino','uid','gid','mode')}):
        return False
    artifact=owner['artifact']
    return (isinstance(artifact,dict) and set(artifact)=={'sha256','size','generation'}
            and type(artifact['size']) is int and 0<artifact['size']<=MAX
            and artifact['size']==current['size']
            and re.fullmatch('[0-9a-f]{64}',str(artifact['sha256'])) is not None
            and re.fullmatch('[0-9a-f]{40}',str(artifact['generation'])) is not None)


@contextlib.contextmanager
def writer(root_fd, fds, inherited=None):
    if sys.platform!='linux' or fcntl is None:raise Refused('regular_cli_platform')
    fd = fds.add(os.open('update.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=root_fd))
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1 or st.st_mode & 0o002:
        raise Refused('regular_cli_lock')
    lock = fd
    if inherited is not None:
        a, b = os.fstat(inherited), os.fstat(fd)
        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino): raise Refused('regular_cli_lock')
        lock = inherited
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if not same_name(root_fd,'update.lock',identity(st)):raise Refused('regular_cli_lock_changed')
    primary=None
    try:yield
    except BaseException as exc:primary=None if isinstance(exc,_Current) else exc;raise
    finally:
        if inherited is None:
            try:fcntl.flock(lock,fcntl.LOCK_UN)
            except BaseException:
                if primary is None:raise


def state_path(root, target):
    return Path(root) / 'regular-cli' / digest(os.fsencode(str(Path(target).absolute())))


def strict_exists(path):
    """Only an actual ENOENT after nofollow traversal means absent."""
    path=Path(path).absolute();fds=FDs();primary=None
    try:
        parent=directory(path.parent,fds)
        os.stat(path.name,dir_fd=parent,follow_symlinks=False)
        return True
    except FileNotFoundError:return False
    except BaseException as exc:primary=exc;raise
    finally:
        try:fds.close()
        except BaseException:
            if primary is None:raise


def selected(root, target=None):
    # Do not turn permission errors, symlinked ancestry, non-directories or
    # close failures into absence and enter the unlocked legacy manual writer.
    path=state_path(root,target) if target is not None else Path(root)/'regular-cli'
    fds=FDs();primary=None
    try:
        directory(path.absolute(),fds)
        return True
    except FileNotFoundError:return False
    except BaseException as exc:primary=exc;raise
    finally:
        try:fds.close()
        except BaseException:
            if primary is None:raise


def install_decision(root, target):
    """Read-only closed decision shared by the normal shell installer."""
    if selected(root,target):return 'owned'
    return 'unmanaged' if strict_exists(target) else 'new'


def publish(data, manifest, target, root, *, create=False, lock_fd=None, clock=time.monotonic, _budget=None):
    budget = _budget or Budget(clock); budget.check(); artifact = verified(data, manifest)
    target, root = Path(target).absolute(), Path(root).absolute()
    if sys.platform != 'linux' or target.name != 'hub': raise Refused('regular_cli_platform_or_target')
    fds = FDs(); primary = None
    try:
        parent = directory(target.parent, fds, create=create)
        lock_root = directory(root, fds, create=True)
        state = directory(state_path(root,target), fds, create=True)
        if os.fstat(state).st_uid != os.getuid() or os.fstat(state).st_mode & 0o077:
            raise Refused('regular_cli_state')
        with writer(lock_root, fds, lock_fd):
            if read_record(state, PENDING, fds) is not None: raise Refused('regular_cli_reconcile_required')
            owner = read_record(state, OWNER, fds)
            try: _, old, before = file_bytes(parent, target.name, fds)
            except FileNotFoundError: old, before = None, None
            if owner is None:
                if before is not None or not create: return {'status':'unmanaged'}
            elif not owner_matches(owner, target, parent, before) or digest(old) != owner['artifact'].get('sha256'):
                raise Refused('regular_cli_owner_changed')
            if owner is not None and owner['artifact']==artifact and old==data:
                if not verified_name(parent,target.name,before,artifact['sha256'],fds):raise Refused('regular_cli_target_changed')
                budget.check()
                raise _Current()
            budget.check()
            name = '.hub-managed-' + str(uuid.uuid4())
            fd = fds.add(os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                0o700, dir_fd=parent))
            write_all(fd,data,budget)
            os.fchmod(fd, stat.S_IMODE(before['mode']) if before else 0o755)
            if before: os.fchown(fd, before['uid'], before['gid'])
            os.fsync(fd);after = identity(os.fstat(fd));os.fsync(parent)
            record = {'schema':1,'target':str(target),'parent':{k:identity(os.fstat(parent))[k] for k in ('dev','ino','uid','gid','mode')},'file':after,'artifact':artifact}
            pending = {'target':str(target),'stage':name,'before':before,'after':after,'owner_before':owner,'owner_after':record}
            if os.fstat(directory(state_path(root,target),fds))!=os.fstat(state):raise Refused('regular_cli_state_changed')
            write_record(state, PENDING, pending, fds, budget, exclusive=True)
            if before is not None and not verified_name(parent,target.name,before,digest(old),fds):raise Refused('regular_cli_target_changed')
            check_parent=directory(target.parent,fds)
            if os.fstat(check_parent)!=os.fstat(parent):raise Refused('regular_cli_parent_changed')
            if os.fstat(directory(root,fds))!=os.fstat(lock_root) or os.fstat(directory(state_path(root,target),fds))!=os.fstat(state):raise Refused('regular_cli_state_changed')
            budget.check()
            exchange(parent, name, target.name, 2 if before else 1)
            os.fsync(parent)
            if (not verified_name(parent,target.name,after,artifact['sha256'],fds)
                    or before is not None and not verified_name(parent,name,before,digest(old),fds)):
                raise Refused('regular_cli_exchange_unknown')
            check_parent=directory(target.parent,fds)
            if os.fstat(check_parent)!=os.fstat(parent):raise Refused('regular_cli_parent_changed')
            write_record(state, OWNER, record, fds, budget)
            # Durable factual transaction receipt, never a health/PASS assertion.
            write_record(state,'transaction-'+name.removeprefix('.hub-managed-')+'.json',
                         {'phase':'published_unqualified','transaction':pending},fds,budget,exclusive=True)
            # Stage is the retained exact original inode; never delete it.
            if os.fstat(directory(state_path(root,target),fds))!=os.fstat(state):raise Refused('regular_cli_state_changed')
            budget.check();os.unlink(PENDING, dir_fd=state);os.fsync(state)
            if (not verified_name(parent,target.name,after,artifact['sha256'],fds)
                    or before is not None and not verified_name(parent,name,before,digest(old),fds)):
                raise Refused('regular_cli_final_identity_unknown')
            result = {'status':'published','generation':artifact['generation'],'retained':name if before else None}
    except _Current:
        result={'status':'current','generation':artifact['generation']}
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try: fds.close()
        except BaseException:
            if primary is None: raise
    budget.check()  # After ALL started durability and close attempts, before success.
    return result


def reconcile(target, root, *, lock_fd=None, clock=time.monotonic, _budget=None):
    """Reconcile only an exact retained own publication; never enroll or replay it."""
    budget=_budget or Budget(clock);budget.check();fds=FDs();primary=None
    try:
        target=Path(target).absolute();root=Path(root).absolute()
        parent=directory(target.parent,fds);lock_root=directory(root,fds)
        state=directory(state_path(root,target),fds)
        with writer(lock_root,fds,lock_fd):
            pending=read_record(state,PENDING,fds)
            if (not isinstance(pending,dict) or set(pending)!={'target','stage','before','after','owner_before','owner_after'}
                    or pending['target']!=str(target) or not re.fullmatch(r'\.hub-managed-[0-9a-f-]{36}',str(pending['stage']))):
                raise Refused('regular_cli_reconcile_unknown')
            before,after=pending['before'],pending['after']
            old,new=pending['owner_before'],pending['owner_after']
            if (not owner_matches(new,target,parent,after) or new['artifact'].get('size')!=after.get('size')
                    or not re.fullmatch('[0-9a-f]{64}',str(new['artifact'].get('sha256')))
                    or not re.fullmatch('[0-9a-f]{40}',str(new['artifact'].get('generation')))):
                raise Refused('regular_cli_reconcile_unknown')
            current=read_record(state,OWNER,fds)
            if current not in (old,new):raise Refused('regular_cli_reconcile_changed')
            stage=pending['stage']
            new_named=verified_name(parent,target.name,after,new['artifact']['sha256'],fds)
            old_retained=(before is None and not strict_exists(target.parent/stage)) or (
                before is not None and isinstance(old,dict) and owner_matches(old,target,parent,before)
                and verified_name(parent,stage,before,old['artifact']['sha256'],fds))
            unchanged=(before is None and not strict_exists(target)) or (
                before is not None and isinstance(old,dict) and owner_matches(old,target,parent,before)
                and verified_name(parent,target.name,before,old['artifact']['sha256'],fds))
            staged=verified_name(parent,stage,after,new['artifact']['sha256'],fds)
            if new_named and old_retained:
                if os.fstat(directory(state_path(root,target),fds))!=os.fstat(state):raise Refused('regular_cli_state_changed')
                write_record(state,OWNER,new,fds,budget)
                result={'status':'reconciled_published'}
            elif unchanged and staged and current==old:
                result={'status':'not_published'}
            else:raise Refused('regular_cli_reconcile_changed')
            # No target publication, rollback, stage deletion or retry here.
            if os.fstat(directory(state_path(root,target),fds))!=os.fstat(state):raise Refused('regular_cli_state_changed')
            budget.check();os.unlink(PENDING,dir_fd=state);os.fsync(state)
            if result['status']=='reconciled_published':
                if not verified_name(parent,target.name,after,new['artifact']['sha256'],fds):raise Refused('regular_cli_reconcile_changed')
            elif (not verified_name(parent,stage,after,new['artifact']['sha256'],fds)
                    or before is not None and not verified_name(parent,target.name,before,old['artifact']['sha256'],fds)
                    or before is None and strict_exists(target)):
                raise Refused('regular_cli_reconcile_changed')
    except BaseException as exc:primary=exc;raise
    finally:
        try:fds.close()
        except BaseException:
            if primary is None:raise
    budget.check()
    return result


def rollback(target, root, pending, *, lock_fd=None, clock=time.monotonic):
    """Explicit conditional rollback of this exact retained transaction only.

    Never called automatically on ambiguity; foreign names remain untouched.
    """
    budget=Budget(clock);fds=FDs();primary=None
    try:
        target=Path(target).absolute();parent=directory(target.parent,fds)
        lock_root=directory(Path(root).absolute(),fds);state=directory(state_path(root,target),fds)
        with writer(lock_root,fds,lock_fd):
            actual=read_record(state,PENDING,fds)
            if (not isinstance(actual,dict) or actual!=pending or actual.get('target')!=str(target)
                    or not owner_matches(actual.get('owner_before'),target,parent,actual.get('before'))
                    or not owner_matches(actual.get('owner_after'),target,parent,actual.get('after'))
                    or not re.fullmatch(r'\.hub-managed-[0-9a-f-]{36}',str(actual.get('stage')))):
                raise Refused('regular_cli_rollback_unknown')
            if not verified_name(parent,target.name,actual['after'],actual['owner_after']['artifact']['sha256'],fds) or not verified_name(parent,actual['stage'],actual['before'],actual['owner_before']['artifact']['sha256'],fds):
                raise Refused('regular_cli_rollback_changed')
            if os.fstat(directory(target.parent,fds))!=os.fstat(parent) or os.fstat(directory(state_path(root,target),fds))!=os.fstat(state):raise Refused('regular_cli_parent_changed')
            budget.check();exchange(parent,target.name,actual['stage']);os.fsync(parent)
            if not verified_name(parent,target.name,actual['before'],actual['owner_before']['artifact']['sha256'],fds) or not verified_name(parent,actual['stage'],actual['after'],actual['owner_after']['artifact']['sha256'],fds):
                raise Refused('regular_cli_rollback_unknown')
            write_record(state,OWNER,actual['owner_before'],fds,budget)
            budget.check();os.unlink(PENDING,dir_fd=state);os.fsync(state)
            if not verified_name(parent,target.name,actual['before'],actual['owner_before']['artifact']['sha256'],fds):raise Refused('regular_cli_rollback_unknown')
    except BaseException as exc:primary=exc;raise
    finally:
        try:fds.close()
        except BaseException:
            if primary is None:raise
    budget.check()
    return {'status':'rolled_back'}


def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',required=True,type=Path);parser.add_argument('--target',type=Path)
    parser.add_argument('--archive',required=True,type=Path);parser.add_argument('--manifest',required=True,type=Path)
    parser.add_argument('--create',action='store_true');parser.add_argument('--lock-fd',type=int)
    parser.add_argument('--select-install',action='store_true')
    args=parser.parse_args()
    # Public code/manifest only. No configuration, credentials or profile lookup.
    budget=Budget();fds=FDs()
    try:
        parent=directory(args.archive.absolute().parent,fds)
        _,data,_=file_bytes(parent,args.archive.name,fds)
        budget.check()
        parent=directory(args.manifest.absolute().parent,fds)
        _,raw,_=file_bytes(parent,args.manifest.name,fds,131072)
        manifest=json.loads(raw)
    finally:fds.close()
    budget.check()
    if args.select_install:
        if args.target is None or args.create or args.lock_fd is not None:raise Refused('regular_cli_selection')
        verified(data,manifest)
        decision=install_decision(args.root,args.target)
        budget.check()
        print(decision)
        return 0
    targets=[args.target] if args.target is not None else []
    if args.target is None:
        # Bounded owned-component records only, never PATH or fleet discovery.
        fds=FDs()
        try:
            directory((args.root/'regular-cli').absolute(),fds)
            children=list((args.root/'regular-cli').iterdir())
            if len(children)>8:raise Refused('regular_cli_selection_bound')
            for child in children:
                if not re.fullmatch('[0-9a-f]{64}',child.name):raise Refused('regular_cli_selection')
                state=directory(child.absolute(),fds)
                owner=read_record(state,OWNER,fds)
                pending=read_record(state,PENDING,fds)
                selected=owner if owner is not None else pending
                if not isinstance(selected,dict) or type(selected.get('target')) is not str:
                    raise Refused('regular_cli_owner_changed')
                target=Path(selected['target'])
                if not target.is_absolute() or state_path(args.root,target)!=child:
                    raise Refused('regular_cli_selection')
                targets.append(target)
        finally:fds.close()
    if not targets:
        print(json.dumps({'status':'unmanaged'}));return 2
    results=[]
    for target in targets:
        budget.check()
        results.append(publish(data,manifest,target,args.root,create=args.create,
                               lock_fd=args.lock_fd,_budget=budget))
    budget.check()
    result={'status':'published' if all(x['status'] in ('published','current') for x in results) else 'unmanaged'}
    print(json.dumps(result))
    return 0 if result['status'] in ('published','current') else 2


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:raise SystemExit('regular_cli_unknown_or_refused')
