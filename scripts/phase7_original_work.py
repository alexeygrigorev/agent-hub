"""Original Phase7 source-audit evidence; no provider/renderer protocol guesses.

The per-agent copy is data-pinned by the caller and runs ONLY with --work.
Importing this module performs no I/O. An artifact plus observed native output
supports the original work criterion, not a provider tool-frame/FormalH claim.
"""
import ast
import hashlib
import json
import os
import stat
import re
import shlex
import subprocess
import time
from pathlib import Path

SOURCE = 'host/agent_hub_host/session_control.py'
ARTIFACT_LIMIT = 8192


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate_key')
        value[key] = item
    return value


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def source_result(raw):
    tree = ast.parse(raw)
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ('MAX_BRIEF', 'MAX_BRIEF_CHARS'):
                    value = ast.literal_eval(node.value)
                    if type(value) is not int or value <= 0:
                        raise ValueError('source_bounds_unknown')
                    values[target.id] = value
    if set(values) != {'MAX_BRIEF', 'MAX_BRIEF_CHARS'}:
        raise ValueError('source_bounds_unknown')
    finding = 'Brief input bounds: %d bytes and %d characters.' % (values['MAX_BRIEF'], values['MAX_BRIEF_CHARS'])
    return {'source_path': SOURCE, 'source_sha256': digest(raw), 'source_bytes': len(raw), 'finding': finding}


def result_line(work):
    return 'PHASE7_SOURCE %s %d %s' % (work['source_sha256'], work['source_bytes'], work['finding'])


def relative_artifact(run_id, sample):
    return Path('.local') / 'phase7-original' / run_id / ('sample-%d.json' % sample)


def private_read(path, limit):
    path = Path(path)
    # This is an owned local workspace, never a profile/config search.
    for parent in reversed(path.absolute().parents):
        if parent.is_symlink():
            raise ValueError('symlink_parent')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ValueError('file_shape')
        chunks = []
        size = 0
        while True:
            block = os.read(fd, min(65536, limit + 1))
            if not block:
                break
            size += len(block)
            if size > limit:
                raise ValueError('file_capacity')
            chunks.append(block)
        after = os.fstat(fd)
        named = os.lstat(path)
        attrs = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if any(getattr(before, k) != getattr(after, k) for k in attrs) or (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('file_changed')
        return b''.join(chunks), before
    finally:
        os.close(fd)


def write_once(path, raw):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def work(context, helper_path):
    """Called by the new agent as useful read-only source work, not by Root."""
    root = Path.cwd().resolve()
    proc = subprocess.run([context['native_client'], 'whoami', '--json'], capture_output=True, text=True, timeout=15)
    if proc.returncode:
        raise ValueError('own_identity_unknown')
    identity = json.loads(proc.stdout)
    if identity.get('workspace') != str(root) or not isinstance(identity.get('id'), str):
        raise ValueError('own_identity_unknown')
    expected_tag = 'task-hub2-' + context['session_id'] if context.get('session_id') else None
    # Root seeds no session id before start. The actual native id is checked
    # against the authenticated Hub tuple by Root after the draw, not trusted here.
    if expected_tag and identity.get('tag') != expected_tag:
        raise ValueError('own_identity_conflict')
    raw, _ = private_read(root / SOURCE, 2 * 1024 * 1024)
    result = {**context, **source_result(raw), 'native_id': identity['id'],
              'native_tag': identity.get('tag'), 'workspace': str(root), 'observed_at_ms': int(time.time() * 1000)}
    result.pop('native_client')
    artifact = root / relative_artifact(context['run_id'], context['sample'])
    for parent in reversed(artifact.parent.relative_to(root).parents):
        candidate = root / parent
        if candidate.exists() and candidate.is_symlink():
            raise ValueError('artifact_parent_unknown')
    artifact.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    # Never chmod existing workspace directories or follow pre-existing links.
    for parent in (artifact.parent, artifact.parent.parent):
        if parent.is_symlink() or parent.stat().st_uid != os.geteuid():
            raise ValueError('artifact_parent_unknown')
    write_once(artifact, (json.dumps(result, sort_keys=True) + '\n').encode())
    print(result_line(result), flush=True)


def brief(run_id, sample, parent, helper_path):
    command = shlex.join(['python3', str(helper_path), '--work'])
    text = (
        'Perform a useful read-only audit of the operator CLI brief-input bounds. '
        'Follow the maintained FIRST_ACTION instruction first. '
        'In your actual assigned workspace, run this reviewed source-audit helper as a foreground tool command: '
        '`%s`. It reads only public %s, computes its SHA256/byte size and '
        'the two declared brief-input bounds, writes one private result artifact bound to this task, '
        'and prints the derived source result. Do not edit the helper, public source, context or result file; '
        'do not invent/echo the result. Let the real command complete. '
        'Then remain available for normal Root STOP; do not start another agent, send to yourself or finish/close the intake. '
        'Run %s sample %d, parent intake %s. No provider/config/auth/environment inventory or other work.'
    ) % (command, SOURCE, run_id, sample, parent)
    return text, 'Read the public source; independently derived SHA/size/bounds appear in actual native capture and private artifact. Remain running for owned STOP.'


def seed(core, helper_path, child_id, parent, run_id, sample, native_client):
    raw = Path(__file__).read_bytes()
    core.exclusive_write_private(helper_path, raw)
    context = {'run_id': run_id, 'sample': sample, 'parent_intake_id': parent,
               'child_intake_id': child_id, 'native_client': native_client}
    core.exclusive_write_private(Path(helper_path).with_suffix('.json'), core.canonical_json_bytes(context))
    return raw


def checked_evidence(path, commit):
    """Only original same-commit canary/e2e receipts, no renderer bundle gate."""
    try:
        raw, _ = private_read(path, 128000)
        value = json.loads(raw)
        if value['commit'] != commit or not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40}', commit):
            raise ValueError('commit_mismatch')
        for name in ('canary', 'ci_e2e'):
            row = value[name]
            if row['commit'] != commit or row['status'] not in ('success', 'green', 'passed', 'pass'):
                raise ValueError('same_commit_not_green')
            data, _ = private_read(row['path'], 2 * 1024 * 1024)
            if digest(data) != row['sha256']:
                raise ValueError('receipt_changed')
        return [], digest(raw), {}
    except (OSError, ValueError, TypeError, KeyError):
        return ['same-commit canary/CI evidence unavailable'], None, {}


def verify_work(raw, expected_source, context, identity, captured, not_before_ms=0):
    """Compound original-AC proof, never an invented provider frame parser."""
    value = json.loads(raw, object_pairs_hook=unique_object)
    allowed = set(context) - {'native_client'} | {'source_path', 'source_sha256', 'source_bytes', 'finding',
                                                'native_id', 'native_tag', 'workspace', 'observed_at_ms'}
    if set(value) != allowed:
        raise ValueError('work_shape')
    for key in set(context) - {'native_client'}:
        if type(value[key]) is not type(context[key]) or value[key] != context[key]:
            raise ValueError('work_binding')
    if (value['native_id'] != identity['native_id'] or value['native_tag'] != 'task-hub2-' + identity['session_id']
            or value['workspace'] != identity['workspace']):
        raise ValueError('work_identity')
    if type(value['observed_at_ms']) is not int or not not_before_ms <= value['observed_at_ms'] <= int(time.time() * 1000) + 5000:
        raise ValueError('work_time')
    expected = source_result(expected_source)
    if any(type(value[k]) is not type(v) or value[k] != v for k, v in expected.items()):
        raise ValueError('work_not_derived')
    if not isinstance(captured, str) or result_line(expected) not in captured:
        raise ValueError('capture_unavailable_or_prompt_only')
    return expected


def observe(core, runner, rec, run_id, sample, parent, child_id, helper_path, helper_bytes, clock, sleep):
    workspace = rec['workspace']
    identity = {'native_id': rec['native_id'], 'session_id': rec['id'], 'workspace': workspace}
    deadline = clock() + core.CAPTURE_BUDGET_S
    # Root computes the expected result from exact immutable selected commit,
    # independently of helper execution/artifact and without putting it in prompt.
    expected = runner.run_bytes('git', ['-C', workspace, 'show', rec['_source_commit'] + ':' + SOURCE], core.left(clock, deadline, 20))
    if expected[0] != 0:
        raise ValueError('selected_source_unknown')
    on_disk, _ = private_read(Path(workspace) / SOURCE, 2 * 1024 * 1024)
    if on_disk != expected[1] or private_read(helper_path, 65536)[0] != helper_bytes:
        raise ValueError('source_or_helper_changed')
    context = {'run_id': run_id, 'sample': sample, 'parent_intake_id': parent, 'child_intake_id': child_id}
    task_path = Path(helper_path).with_suffix('.task.txt')
    write_once(task_path, brief(run_id, sample, parent, helper_path)[0].encode())
    sent = runner.json('hub', ['sessions', 'send', rec['id'], '--key', 'original-work-' + run_id + '-' + str(sample),
                               '--body-file', str(task_path), '--wait', '--timeout', '30'], core.left(clock, deadline, 35))
    receipt = sent.get('command_wait', {})
    if (receipt.get('state') != 'acked' or receipt.get('kind') != 'send'
            or receipt.get('session_id') != rec['id'] or not isinstance(sent.get('command_id'), str)
            or receipt.get('id') != sent['command_id']
            or sent.get('client_key') != 'original-work-' + run_id + '-' + str(sample)):
        raise ValueError('send_authority_unknown')
    artifact = Path(workspace) / relative_artifact(run_id, sample)
    while clock() < deadline:
        # Same owned tuple before/after native capture. Missing authority is
        # UNKNOWN, never repaired with a provider-specific guessed format.
        before = core.session_of(runner.json('hub', ['sessions', 'show', rec['id'], '--json'], core.left(clock, deadline, 15)), rec['id'])
        if before.get('state') != 'running' or core.row_workspace(before) != workspace:
            raise ValueError('capture_not_running')
        if core._bind_view(rec['owned_identity'], before, core.machine_session_identity, 'capture identity', core.IDENTITY_KEYS):
            raise ValueError('capture_identity_changed')
        rc, data, _ = runner.run_bounded('a', ['capture', rec['native_id'], '--screen', '--plain'],
            deadline=deadline, timeout=core.left(clock, deadline, 20), max_stdout=core.CAPTURE_LIMIT, max_stderr=4096)
        try:
            text = data.decode('utf-8')
        except (UnicodeDecodeError, AttributeError):
            raise ValueError('capture_text_unknown')
        after = core.session_of(runner.json('hub', ['sessions', 'show', rec['id'], '--json'], core.left(clock, deadline, 15)), rec['id'])
        if after.get('state') != 'running' or core.row_workspace(after) != workspace:
            raise ValueError('capture_not_running')
        if core._bind_view(rec['owned_identity'], after, core.machine_session_identity, 'capture identity', core.IDENTITY_KEYS):
            raise ValueError('capture_identity_changed')
        if rc != 0:
            raise ValueError('capture_authority_unavailable')
        try:
            raw, meta = private_read(artifact, ARTIFACT_LIMIT)
            if meta.st_uid != os.geteuid() or stat.S_IMODE(meta.st_mode) != 0o600:
                raise ValueError('artifact_owner')
            if meta.st_mtime_ns // 1000000 < rec['_start_wall_ms']:
                raise ValueError('artifact_not_fresh')
            result = verify_work(raw, expected[1], context, identity, text, rec['_start_wall_ms'])
            if private_read(Path(workspace) / SOURCE, 2 * 1024 * 1024)[0] != expected[1]:
                raise ValueError('source_changed')
            # Only hashes/closed source findings leave private capture custody.
            rec.update(capture_verified=True, original_work=result, capture_sha256=digest(text.encode()),
                       artifact_sha256=digest(raw), original_capture_scope='owned_native_text_plus_independent_source_artifact')
            return
        except (OSError, ValueError, TypeError, KeyError):
            sleep(min(1, max(0, deadline - clock())))
    raise ValueError('original_work_unknown')




def public_summary(summary):
    """No raw captures, command stderr, arbitrary exception strings or env."""
    public = {'mode': 'original_ac', 'ok': summary['ok'], 'run_id': summary['run_id'],
              'commit': summary['commit'], 'window': summary['window'],
              'models': summary['models'], 'luna': summary['luna'], 'astra': summary['astra'],
              'problems': [] if summary['ok'] else ['original_requirements_or_custody_unconfirmed'], 'starts': []}
    for row in summary['starts']:
        safe = {k: row.get(k) for k in ('n', 'id', 'child_intake_id', 'native_id', 'engine', 'model', 'host',
                                       'workspace', 'elapsed_s', 'shares', 'capture_verified', 'capture_sha256',
                                       'artifact_sha256', 'cleanup_status', 'cleanup_confirmed')}
        safe['outcome'] = 'UNKNOWN' if row['errors'] else 'OBSERVED'
        public['starts'].append(safe)
    public['alert_watermarks'] = {name: ({'generation_sha256': digest(value['generation'].encode()),
        'count': value['count'], 'observed_at_ms': value['observed_at_ms']} if isinstance(value, dict) else value)
        for name, value in summary['alert_watermarks'].items() if name in ('before', 'after')}
    return public


def retain_and_attach(core, runner, output_dir, intake, text, code, *, attach):
    """Retain ACTUAL sanitized stdout, then attach its actual bytes in keyed notes.

    Failure is retained/PENDING, never a request to repeat the ten draws.
    """
    folder = Path(output_dir)
    if not folder.exists():
        core.make_private_output_dir(folder)
    raw = text.encode('utf-8')
    write_once(folder / 'actual-stdout.txt', raw)
    record = {'status': 'RETAINED_NOT_ATTACHED', 'stdout_sha256': digest(raw), 'bytes': len(raw),
              'core_exit_code': code, 'parts': []}
    write_once(folder / 'stdout-retained.json', (json.dumps(record, sort_keys=True) + '\n').encode())
    if not attach:
        return False
    # Existing plain-note API: <=3000characters and <=12000UTF8bytes per
    # chunk, under its4000character/16000byte limits. Never an output path only.
    chunks = [text[i:i+2500] for i in range(0, len(text), 2500)] or ['']
    run_key = digest(raw)[:32]
    for i, chunk in enumerate(chunks, 1):
        key = 'phase7-output-%s-%d' % (run_key, i)
        note = 'Original Phase7 actual script stdout part %d/%d sha256=%s exit=%d\n%s' % (i, len(chunks), digest(raw), code, chunk)
        path = folder / ('output-note-%d.txt' % i)
        write_once(path, note.encode())
        write_once(folder / ('output-append-%d-intent.json' % i), json.dumps({'key': key, 'note_sha256': digest(note.encode())}).encode())
        try:
            result = runner.json('hub', ['intake', 'append', intake, '--key', key, '--note-file', str(path)], 30)
            if result.get('appended') is not True or result.get('intake_id') != intake:
                raise ValueError('append_unconfirmed')
            write_once(folder / ('output-append-%d-result.json' % i), (json.dumps(result, sort_keys=True) + '\n').encode())
        except Exception:
            write_once(folder / 'output-attachment-pending.json', json.dumps({'status': 'UNKNOWN', 'part': i, 'key': key}).encode())
            return False
        record['parts'].append({'key': key, 'note_sha256': digest(note.encode())})
    record['status'] = 'APPENDED_ACTUAL_OUTPUT'
    write_once(folder / 'output-attachment-complete.json', (json.dumps(record, sort_keys=True) + '\n').encode())
    return True


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--work', action='store_true')
    args = p.parse_args()
    if not args.work:
        raise SystemExit('HOLD: only the new agent executes --work')
    context_path = Path(__file__).with_suffix('.json')
    work(json.loads(private_read(context_path, ARTIFACT_LIMIT)[0]), __file__)
