"""Bounded stdout capture for the exact local-profile launch attempt."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time


def output_parts(task_id, operation_id, epoch):
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", task_id):
        raise RuntimeError('task_output_identity_invalid')
    token = hashlib.sha256(json.dumps([task_id, operation_id, epoch], separators=(',', ':')).encode()).hexdigest()
    return ('.local', 'hub2-output', token)


def open_beneath(workspace, parts, create=False):
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
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_beneath(workspace, parts, limit, tail=False):
    fd = open_beneath(workspace, parts)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError('task_output_file_invalid')
        if not tail and info.st_size > limit:
            raise RuntimeError('task_output_record_too_large')
        if tail:
            os.lseek(fd, max(0, info.st_size - limit), os.SEEK_SET)
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


def capture_task_output(task_id, expected, native_id, limit=16384):
    """None selects legacy/interactive PTY capture; empty means task not ready yet."""
    workspace = expected['workspace']
    try:
        record = json.loads(read_beneath(workspace, ('.local', 'launch-' + task_id + '.json'), 65536))
    except FileNotFoundError:
        return None
    if not isinstance(record, dict):
        raise RuntimeError('task_output_record_invalid')
    binding = record.get('task_output')
    if binding is None:
        return None
    parts = output_parts(task_id, expected['operation_id'], expected['epoch'])
    if (record.get('session_id') != native_id or record.get('tag') != expected['tag']
            or not same_path(record.get('workspace'), workspace)
            or not isinstance(binding, dict) or binding.get('parts') != list(parts)
            or binding.get('operation_id') != expected['operation_id'] or binding.get('epoch') != expected['epoch']):
        raise RuntimeError('task_output_fence_conflict')
    try:
        start = json.loads(read_beneath(workspace, (*parts, 'START.json'), 131072))
    except FileNotFoundError:
        return ''
    if not isinstance(start, dict):
        raise RuntimeError('task_output_record_invalid')
    parent = start.get('parent_session')
    created, started = record.get('created_at_ms'), start.get('started_ms')
    if (not isinstance(parent, dict) or parent.get('id') != native_id or parent.get('tag') != expected['tag']
            or not same_path(parent.get('workspace'), workspace)
            or not same_path(start.get('cwd'), workspace)
            or not same_path(start.get('output_dir'), Path(workspace).resolve().joinpath(*parts))
            or start.get('schema_version') != 1 or type(created) is not int or type(started) is not int
            or not created <= started <= time.time() * 1000 + 5000
            or start.get('profile') != binding.get('profile')
            or not isinstance(start.get('engine'), str) or not start['engine']
            or (binding.get('engine') is not None and start.get('engine') != binding['engine'])
            or start.get('prompt_sha256') != binding.get('prompt_sha256')):
        raise RuntimeError('task_output_start_conflict')
    try:
        return read_beneath(workspace, (*parts, 'stdout.log'), min(16384, max(1, limit)), tail=True)
    except FileNotFoundError:
        return ''


def bounded_capture(output):
    # Command acknowledgements have an 8192-byte JSON ceiling, including escapes.
    output = output.encode('utf8', errors='replace')[-4096:].decode('utf8', errors='replace')
    while len(json.dumps({'output': output}, ensure_ascii=False).encode()) > 8000:
        output = output[max(1, len(output) // 4):]
    return output
