"""Root-pinned per-sample native work helper (rendered bytes are pinned BEFORE dispatch).

The template body is Root's canonical Hydrogen observation-2 probe (sha256
1e25e2d1f97b95dbbcdd8099a88de48e1242383a609cffad2ecab320df94b336) with three
integration changes: no ``expected_head`` binding (the driver independently
verifies the child's actual HEAD/status at proof time and the child worktree
HEAD is not known before start), the driver's existing result location
``.local/acceptance-native/<run>/<sample>/result.json``, and the BINDING key set.
Identity (Hub session from the native tag, native id, workspace) is observed at
runtime from ``aplexer whoami --json`` and only ``id``/``tag``/``workspace`` are
read; the full record is never written, logged or hashed. The helper creates
the result with O_EXCL (the driver never pre-creates it) and prints the one
``AC10_WORK_V2`` line. Rendering is deterministic: the same binding always
yields the same bytes.
"""
import json
import re
import uuid

HEADER = (
    '#!/usr/bin/python3\n'
    '"""Root-pinned Phase 7 native work helper. Copy these exact bytes; do not edit."""\n'
    'import grp, hashlib, json, os, pathlib, pwd, re, selectors, stat, subprocess, time, uuid\n'
    '\n'
)
BINDING_KEYS = ('aplexer_binary', 'challenge_path', 'child_intake_id', 'machine_id',
                'parent_intake_id', 'project', 'run_id', 'sample')
BODY = r'''
def need(ok):
    if not ok: raise ValueError('native work refused')

def unique(pairs):
    out = {}
    for k, v in pairs:
        need(k not in out); out[k] = v
    return out

def canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')

def command(argv, cap):
    # Bounded RAM only. A full public SessionRecord may contain unrelated fields;
    # never write native whoami stdout to a temp/artifact, log it, or hash the document.
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 10
    data = bytearray()
    try:
        with selectors.DefaultSelector() as poll:
            poll.register(p.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic(); need(remaining > 0)
                need(bool(poll.select(remaining)))
                chunk = os.read(p.stdout.fileno(), min(4096, cap + 1 - len(data)))
                if not chunk: break
                data.extend(chunk); need(len(data) <= cap)
        need(p.wait(timeout=max(0.001, deadline - time.monotonic())) == 0)
        return bytes(data)
    finally:
        if p.poll() is None:
            p.kill(); p.wait(timeout=1)
        p.stdout.close()

def whoami_identity(raw):
    need(isinstance(raw, bytes) and len(raw) <= 65536)
    def bad(_): raise ValueError('nonfinite JSON')
    doc = json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=bad)
    need(isinstance(doc, dict) and {'id', 'tag', 'workspace'} <= set(doc))
    # Do not export, hash, log, or preserve any extra fields (command/env/auth/etc.).
    identity = {key: doc[key] for key in ('id', 'tag', 'workspace')}
    need(all(isinstance(v, str) for v in identity.values()))
    need(str(uuid.UUID(identity['id'])) == identity['id'])
    need(re.fullmatch(r'task-hub2-[0-9a-f-]{36}', identity['tag']) is not None)
    need(pathlib.Path(identity['workspace']).is_absolute())
    return identity

def private_group(gid):
    me = pwd.getpwuid(os.geteuid())
    names = set(grp.getgrgid(gid).gr_mem)
    names.update(p.pw_name for p in pwd.getpwall() if p.pw_gid == gid)
    return me.pw_gid == gid and names == {me.pw_name}

def status_count(raw):
    count, pos = 0, 0
    while pos < len(raw):
        end = raw.find(b'\0', pos); need(end >= 0)
        row = raw[pos:end]; need(len(row) >= 4 and row[2:3] == b' ')
        need(all(c in b' MADRCUT?!' for c in row[:2])); pos = end + 1
        if b'R' in row[:2] or b'C' in row[:2]:
            end = raw.find(b'\0', pos); need(end > pos); pos = end + 1
        count += 1
    return count

def main():
    c = BINDING
    need(set(c) == {'run_id', 'sample', 'parent_intake_id', 'child_intake_id', 'project', 'machine_id', 'aplexer_binary', 'challenge_path'})
    need(type(c['sample']) is int and 1 <= c['sample'] <= 10)
    for k in ('run_id', 'parent_intake_id', 'child_intake_id'):
        need(str(uuid.UUID(c[k])) == c[k])
    need(c['project'] == 'alexeygrigorev/agent-hub' and c['machine_id'] == 'hetzner-aplexer')
    local = pathlib.Path.cwd() / '.local'
    m = os.lstat(local)
    need(stat.S_ISDIR(m.st_mode) and m.st_uid == os.geteuid() and not m.st_mode & 0o002)
    need(not m.st_mode & 0o020 or private_group(m.st_gid))
    who = whoami_identity(command([c['aplexer_binary'], 'whoami', '--json'], 65536))
    match = re.fullmatch(r'task-hub2-([0-9a-f-]{36})', who['tag']); need(match is not None)
    sid = match.group(1); need(str(uuid.UUID(sid)) == sid)
    cwd = pathlib.Path.cwd().resolve(strict=True); need(str(cwd) == who['workspace'])
    head = command(['git', '-C', str(cwd), 'rev-parse', '--verify', 'HEAD'], 128)
    need(re.fullmatch(b'[0-9a-f]{40}\n', head) is not None)
    head = head[:-1].decode('ascii')
    status = command(['git', '-C', str(cwd), 'status', '--porcelain=v1', '-z', '--untracked-files=no'], 65536)
    need(str(pathlib.Path(c['challenge_path']).resolve(strict=True)) == c['challenge_path'])
    f = os.open(c['challenge_path'], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        m = os.fstat(f); need(stat.S_ISREG(m.st_mode) and m.st_uid == os.geteuid() and m.st_nlink == 1 and not m.st_mode & 0o077 and m.st_size == 32)
        challenge = os.read(f, 33); need(len(challenge) == 32)
    finally: os.close(f)
    work = dict(schema='phase7_native_work_v2', run_id=c['run_id'], sample=c['sample'],
                parent_intake_id=c['parent_intake_id'], child_intake_id=c['child_intake_id'], session_id=sid,
                native_id=who['id'], project=c['project'], machine_id=c['machine_id'], workspace=str(cwd),
                head=head, tracked_status_sha256=hashlib.sha256(status).hexdigest(),
                tracked_status_entries=status_count(status), calculation=sum(range(1, 101)))
    digest = hashlib.sha256(b'HUB_PHASE7_NATIVE_WORK_V2\0' + challenge + b'\0' + canon(work)).hexdigest()
    fd = os.open(cwd, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in ('.local', 'acceptance-native', c['run_id'], str(c['sample'])):
            try: os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError: pass
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            m = os.fstat(nxt); need(m.st_uid == os.geteuid() and not m.st_mode & 0o002)
            need(not m.st_mode & 0o020 or private_group(m.st_gid))
            os.close(fd); fd = nxt
        out = os.open('result.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(out, 'wb') as stream:
            stream.write(canon({**work, 'proof_digest': digest}) + b'\n'); stream.flush(); os.fsync(stream.fileno())
        os.fsync(fd)
    finally: os.close(fd)
    print(f'AC10_WORK_V2 {c["run_id"]} {c["sample"]} {digest} {head} 5050', flush=True)

if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Native work refused; no credential/command output recorded.') from None
'''


def render_helper(binding):
    """Return the exact helper bytes for one sample's binding (no I/O)."""
    if not isinstance(binding, dict) or set(binding) != set(BINDING_KEYS):
        raise ValueError('helper binding keys invalid')
    if type(binding['sample']) is not int or not 1 <= binding['sample'] <= 10:
        raise ValueError('helper sample invalid')
    for key in ('run_id', 'parent_intake_id', 'child_intake_id'):
        if not isinstance(binding[key], str) or str(uuid.UUID(binding[key])) != binding[key]:
            raise ValueError('helper %s invalid' % key)
    for key in ('aplexer_binary', 'challenge_path'):
        value = binding[key]
        if (not isinstance(value, str) or not value.startswith('/') or '..' in value.split('/')
                or re.search(r'[\x00-\x1f\x7f\'"\\\\]', value)):
            raise ValueError('helper %s invalid' % key)
    if binding['project'] != 'alexeygrigorev/agent-hub' or binding['machine_id'] != 'hetzner-aplexer':
        raise ValueError('helper project/machine invalid')
    line = json.dumps(binding, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return (HEADER + 'BINDING = json.loads(%r)\n' % line + BODY).encode('utf-8')
