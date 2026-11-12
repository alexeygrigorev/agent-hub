"""Bounded stdout capture for the exact local-profile launch attempt."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time


# Capture travels in durable HTTP receipts, never in a websocket frame.
CAPTURE_OUTPUT_BYTES = 64 * 1024
# A control byte can take six JSON bytes; leave room for the <=1KiB binding.
CAPTURE_RESULT_JSON_BYTES = 400 * 1024
CAPTURE_ACK_BODY_BYTES = 401 * 1024


def capture_tail(output, limit=CAPTURE_OUTPUT_BYTES):
    return output.encode('utf8', errors='replace')[-limit:].decode('utf8', errors='ignore')


def output_parts(task_id, operation_id, epoch):
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", task_id):
        raise RuntimeError('task_output_identity_invalid')
    token = hashlib.sha256(json.dumps([task_id, operation_id, epoch], separators=(',', ':')).encode()).hexdigest()
    return ('.local', 'hub2-output', token)


def open_beneath(workspace, parts, create=False, private=False):
    """Open fixed relative components without following file/directory symlinks."""
    if not parts or any(not isinstance(p, str) or p in ('', '.', '..') or '/' in p or '\\' in p for p in parts):
        raise RuntimeError('task_output_path_invalid')
    root = Path(workspace).resolve(strict=True)
    if os.open not in os.supports_dir_fd or not hasattr(os, 'O_NOFOLLOW'):
        raise RuntimeError('task_output_platform_unsupported')
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(root, flags | os.O_DIRECTORY)
    try:
        for index, part in enumerate(parts):
            directory = create or index < len(parts) - 1
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, flags | (os.O_DIRECTORY if directory else 0), dir_fd=fd)
            if private and directory:
                info = os.fstat(child)
                # The retained attempt directory is the privacy boundary: task
                # stdout may be 0664, but must be unreachable outside this 0700
                # owner directory, with no hardlink elsewhere.
                # Existing owned ancestors may be group writable. Descriptor
                # traversal pins them without following links; privacy is
                # enforced at the exact 0700 attempt boundary and bound records.
                if (info.st_uid != os.geteuid()
                        or index < 2 and info.st_mode & 0o002
                        or index == 2 and stat.S_IMODE(info.st_mode) != 0o700):
                    os.close(child)
                    raise RuntimeError('task_output_private_path_required')
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_beneath(workspace, parts, limit, tail=False, private=False):
    fd = open_beneath(workspace, parts, private=private)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or private and (info.st_uid != os.geteuid() or info.st_nlink != 1
                or not tail and info.st_mode & 0o077):
            raise RuntimeError('task_output_file_invalid')
        if not tail and info.st_size > limit:
            raise RuntimeError('task_output_record_too_large')
        if tail:
            # Include enough preceding bytes to decode a split UTF-8 character
            # before taking the byte tail. Invalid interior bytes still replace.
            os.lseek(fd, max(0, info.st_size - limit - 3), os.SEEK_SET)
            return capture_tail(os.read(fd, limit + 3).decode('utf8', errors='replace'), limit)
        return os.read(fd, limit).decode('utf8', errors='replace')
    finally:
        os.close(fd)


def prepare_output(task_id, specification, profile, engine, prompt):
    if os.open not in os.supports_dir_fd or not hasattr(os, 'O_NOFOLLOW'):
        return None  # Keep the existing launch path on platforms without safe descriptor traversal.
    parts = output_parts(task_id, specification.get('operation_id'), specification.get('epoch'))
    fd = open_beneath(specification['workspace'], parts, create=True)
    try:
        if os.listdir(fd):
            raise RuntimeError('task_output_attempt_exists')
    finally:
        os.close(fd)
    return {'parts': list(parts), 'profile': profile, 'engine': engine,
            'operation_id': specification.get('operation_id'), 'epoch': specification.get('epoch'),
            'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest()}


def same_path(value, expected):
    try:
        return isinstance(value, str) and Path(value).resolve() == Path(expected).resolve()
    except (ValueError, OSError):
        return False


def capture_task_output(task_id, expected, native_id, limit=CAPTURE_OUTPUT_BYTES, *, retained_only=False):
    """None selects legacy/interactive PTY capture; empty means task not ready yet."""
    if os.open not in os.supports_dir_fd or not hasattr(os, 'O_NOFOLLOW'):
        if retained_only:
            raise RuntimeError('task_output_unavailable')
        return None
    workspace = expected['workspace']
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise RuntimeError('task_output_record_invalid')
            value[key] = item
        return value
    try:
        record = json.loads(read_beneath(workspace, ('.local', 'launch-' + task_id + '.json'), 65536,
            private=retained_only), object_pairs_hook=unique)
    except FileNotFoundError:
        if retained_only:
            raise RuntimeError('task_output_unavailable') from None
        return None
    if not isinstance(record, dict):
        raise RuntimeError('task_output_record_invalid')
    binding = record.get('task_output')
    if binding is None:
        if retained_only:
            raise RuntimeError('task_output_unavailable')
        return None
    parts = output_parts(task_id, expected['operation_id'], expected['epoch'])
    if (record.get('session_id') != native_id or record.get('tag') != expected['tag']
            or not same_path(record.get('workspace'), workspace)
            or not isinstance(binding, dict) or binding.get('parts') != list(parts)
            or binding.get('operation_id') != expected['operation_id'] or type(binding.get('epoch')) is not int or binding.get('epoch') != expected['epoch']):
        raise RuntimeError('task_output_fence_conflict')
    try:
        start = json.loads(read_beneath(workspace, (*parts, 'START.json'), 131072, private=retained_only), object_pairs_hook=unique)
    except FileNotFoundError:
        if retained_only:
            raise RuntimeError('task_output_unavailable') from None
        return ''
    if not isinstance(start, dict):
        raise RuntimeError('task_output_record_invalid')
    parent = start.get('parent_session')
    created, started = record.get('created_at_ms'), start.get('started_ms')
    if (not isinstance(parent, dict) or parent.get('id') != native_id or parent.get('tag') != expected['tag']
            or not same_path(parent.get('workspace'), workspace)
            or not same_path(start.get('cwd'), workspace)
            or not same_path(start.get('output_dir'), Path(workspace).resolve().joinpath(*parts))
            or type(start.get('schema_version')) is not int or start.get('schema_version') != 1 or type(created) is not int or type(started) is not int
            or not created <= started <= time.time() * 1000 + 5000
            or start.get('profile') != binding.get('profile')
            or not isinstance(start.get('engine'), str) or not start['engine']
            or (binding.get('engine') is not None and start.get('engine') != binding['engine'])
            or not isinstance(binding.get('prompt_sha256'), str) or not re.fullmatch('[0-9a-f]{64}',binding['prompt_sha256'])
            or start.get('prompt_sha256') != binding.get('prompt_sha256')):
        raise RuntimeError('task_output_start_conflict')
    try:
        return read_beneath(workspace, (*parts, 'stdout.log'), min(CAPTURE_OUTPUT_BYTES, max(1, limit)), tail=True, private=retained_only)
    except FileNotFoundError:
        if retained_only:
            raise RuntimeError('task_output_unavailable') from None
        return ''


def bounded_capture(output, result_fields=None):
    # Keep raw UTF-8 and serialized JSON budgets independent, including escapes.
    if result_fields and len(json.dumps(result_fields,ensure_ascii=True).encode())>1024:
        raise RuntimeError('capture_fence_conflict')
    output = capture_tail(output)
    while len(json.dumps({**(result_fields or {}), 'output': output}, ensure_ascii=True).encode()) > CAPTURE_RESULT_JSON_BYTES:
        output = output[max(1, len(output) // 4):]
    return output
